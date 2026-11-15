"""包邮门槛附近的凑单聚集研究。

计量口径固定在资料约定上：

* 运行变量是订单的 ``qualifying_amount``：顾客确认时的计费资格金额，即商品促销后、
  配送券前、事后退款前。配送券与退款只进入经营解释，不会把订单改分到门槛另一侧。
* 门槛由订单上的 ``rule_id`` 锁定，按规则生效期解释历史，不按查询当天或资料中最新的
  门槛重算；同一门槛可以来自不同地区、不同日期的多条规则。
* 金额按十进制“分”精确分箱；浮点数只用于拟合与区间，不参与金额对账。频数单位是订单，
  不按商品行扩张。
* 观测金额只落在少量“商品单价×数量”刻度上。围绕门槛的连续“平滑反事实”在这种刻度
  上没有直接意义，因此反事实以刻度（商品构成）模型为主，平滑模型只作对照。

识别策略：主反事实用商品价格×数量的独立性模型，但用**窗口外**的格估计边际，窗口内
作留出预测；另给一条把窗口包含进边际的保守变体作为下界。只有当保守下界仍显著为正，
且规则外虚假门槛、另一门槛期同一刻度、跨门槛期留出反事实和规格曲线都不与它冲突时，
才报告“有支持”；否则报告“识别不足”，并且不把聚集量当作新增订单、新增销售或收益承诺。
"""

import math
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from scipy import stats as sps

CENT = Decimal("0.01")
LATTICE_MODELS = ("composition", "composition_excluded")
SMOOTH_MODELS = ("poisson1", "poisson2")
MODELS = LATTICE_MODELS + SMOOTH_MODELS
DEFAULT_WINDOW = 12.0
DEFAULT_EXCLUSION = 20.0
DEFAULT_MODEL = "composition"
WINDOW_GRID = (5.0, 10.0, 12.0, 15.0, 20.0)
EXCLUSION_GRID = (12.0, 15.0, 20.0, 25.0, 30.0)
SIGNIFICANCE = 0.05
PLACEBO_MIDDLE = (0.10, 0.90)


class StudyError(ValueError):
    """研究参数或资料不支持该口径。"""


# ---------------------------------------------------------------------------
# 数值与格式


def _cents(value):
    return int((Decimal(value) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _money(cents):
    return format((Decimal(cents) / 100).quantize(CENT), "f")


def _number(value, digits=4):
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _quantile(values, fraction):
    return float(np.quantile(np.asarray(values, dtype=float), fraction)) if values else None


def _share(count, total):
    return count / total if total else 0.0


def _normal_quantile(probability):
    return float(sps.norm.ppf(probability))


def _upper_p(observed, expected):
    """观察值相对反事实偏高单侧检验（Poisson 方差近似）。"""
    total = observed + expected
    return float(sps.norm.sf((observed - expected) / math.sqrt(total))) if total > 0 else None


# ---------------------------------------------------------------------------
# 资料读取与完整性


def _load(catalog):
    orders = catalog.rows("orders")
    rules = {record["id"]: record for record in catalog.rows("shipping_rules")}
    lines_by_order = defaultdict(list)
    for line in catalog.rows("order_lines"):
        lines_by_order[line["order_id"]].append(line)
    settlements = {}
    duplicates = 0
    for settlement in catalog.rows("settlements"):
        if settlement["order_id"] in settlements:
            duplicates += 1
        settlements[settlement["order_id"]] = settlement
    return {"orders": orders, "rules": rules, "lines_by_order": lines_by_order,
            "settlements": settlements, "duplicate_settlements": duplicates}


def _integrity(loaded):
    rules = loaded["rules"]
    problems = Counter()
    examples = []

    def note(kind, order_id):
        problems[kind] += 1
        if len(examples) < 8:
            examples.append({"kind": kind, "order_id": order_id})

    for order in loaded["orders"]:
        rule = rules.get(order["rule_id"])
        if rule is None:
            note("unknown_rule", order["id"])
            continue
        if rule["region"] != order["region"]:
            note("region_mismatch", order["id"])
        if not rule["valid_from"] <= order["created_at"][:10] < rule["valid_to"]:
            note("date_outside_rule", order["id"])
        lines = loaded["lines_by_order"].get(order["id"], [])
        if lines and sum(_cents(line["unit_price"]) * int(line["quantity"]) - _cents(line["discount"])
                         for line in lines) != _cents(order["qualifying_amount"]):
            note("line_sum_mismatch", order["id"])
        if order["id"] not in loaded["settlements"]:
            note("missing_settlement", order["id"])
    problems["duplicate_settlement"] = loaded["duplicate_settlements"]
    for kind in ("unknown_rule", "region_mismatch", "date_outside_rule",
                 "line_sum_mismatch", "missing_settlement", "duplicate_settlement"):
        problems.setdefault(kind, 0)
    return {"problems": dict(sorted(problems.items())), "examples": examples,
            "clean": not any(problems.values())}


# ---------------------------------------------------------------------------
# 金额刻度与商品价格结构


def _scale(amounts):
    unique = sorted(set(amounts))
    if not unique:
        return {"unique_amounts": 0, "minimum_step": None, "minimum_step_cents": None,
                "all_whole_yuan": None, "minimum": None, "maximum": None}
    step = 0
    for left, right in zip(unique, unique[1:]):
        step = math.gcd(step, right - left)
    return {
        "unique_amounts": len(unique),
        "minimum_step_cents": step or None,
        "minimum_step": _money(step) if step else None,
        "all_whole_yuan": None if not step else all(amount % 100 == 0 for amount in unique),
        "minimum": _money(unique[0]),
        "maximum": _money(unique[-1]),
        "stored_increment": "0.01",
        "note": ("资料以十进制分保存，但可观测成交金额只落在少量刻度上；在刻度上求平均密度或"
                 "连续平滑没有直接意义，需与刻度构成模型对照。"),
    }


def _price_structure(records, band_lo, band_hi):
    outside = [record for record in records if not band_lo <= record["amount_c"] < band_hi]
    prices = sorted({record["price_c"] for record in records})
    quantities = sorted({record["qty"] for record in records})
    price_counts = Counter(record["price_c"] for record in outside)
    qty_counts = Counter(record["qty"] for record in outside)
    table = np.zeros((len(prices), len(quantities)), dtype=float)
    for record in outside:
        table[prices.index(record["price_c"]), quantities.index(record["qty"])] += 1
    observed_cells = Counter((record["price_c"], record["qty"]) for record in records)
    masked = [{"unit_price": _money(price), "quantity": qty, "amount": _money(price * qty)}
              for price in prices for qty in quantities
              if band_lo <= price * qty < band_hi and observed_cells[(price, qty)] > 0]
    statistic = degrees = p_value = minimum_expected = None
    if table.sum() > 0 and len(prices) > 1 and len(quantities) > 1:
        expected = table.sum() * np.outer(table.sum(axis=1) / table.sum(),
                                          table.sum(axis=0) / table.sum())
        with np.errstate(divide="ignore", invalid="ignore"):
            statistic = float(np.nansum((table - expected) ** 2 / np.where(expected > 0, expected, np.nan)))
        degrees = (len(prices) - 1) * (len(quantities) - 1)
        p_value = float(sps.chi2.sf(statistic, degrees)) if degrees else None
        minimum_expected = float(expected.min())
    collisions = sorted(amount for amount, count in Counter(
        price * qty for price in prices for qty in quantities).items() if count > 1)
    missing_prices = [price for price in prices if price_counts.get(price, 0) == 0]
    missing_quantities = [qty for qty in quantities if qty_counts.get(qty, 0) == 0]
    return {
        "unit_prices": [_money(price) for price in prices],
        "quantities": quantities,
        "price_counts_outside_band": {_money(price): price_counts.get(price, 0) for price in prices},
        "quantity_counts_outside_band": {str(qty): qty_counts.get(qty, 0) for qty in quantities},
        "price_quantity_cells": len(prices) * len(quantities),
        "distinct_amounts_from_cells": len({price * qty for price in prices for qty in quantities}),
        "colliding_amounts": [_money(amount) for amount in collisions],
        "masked_cells_in_band": masked,
        "outside_identifiable": not missing_prices and not missing_quantities,
        "outside_missing_prices": [_money(price) for price in missing_prices],
        "outside_missing_quantities": missing_quantities,
        "independence_chi_square": _number(statistic),
        "independence_degrees": degrees,
        "independence_p_value": _number(p_value, 6),
        "minimum_expected_count": _number(minimum_expected, 2),
        "outside_records": len(outside),
        "inside_records": len(records) - len(outside),
        "note": ("独立性检验与窗口外构成模型都只用排除带外的格。带内若遮住整行或整列商品格，"
                 "窗口外口径会不可识别，此时只用保守下界报告。"),
    }


def _attainable(records):
    prices = sorted({record["price_c"] for record in records})
    quantities = sorted({record["qty"] for record in records})
    return sorted({price * qty for price in prices for qty in quantities})


# ---------------------------------------------------------------------------
# 反事实：刻度构成模型与连续平滑模型


def _composition_marginals(records):
    return Counter(record["price_c"] for record in records), Counter(record["qty"] for record in records)


def _independent_prediction(price_counts, qty_counts, total):
    price_total = sum(price_counts.values())
    qty_total = sum(qty_counts.values())
    if not price_total or not qty_total:
        return None
    expected = Counter()
    for price, price_count in price_counts.items():
        for qty, qty_count in qty_counts.items():
            expected[price * qty] += total * (price_count / price_total) * (qty_count / qty_total)
    return expected


def _calibrate(expected, total):
    ordered = sum(expected.values())
    if ordered > 0 and abs(ordered - total) > 1e-9:
        return Counter({amount: value * total / ordered for amount, value in expected.items()})
    return expected


def _composition(records, exclusion_lo, exclusion_hi, use_outside):
    """商品价格×数量构成反事实。

    ``composition``（全样本边际）：价格与数量边际由全部订单估计。门槛窗口里的凑单会同时
    抬高价格与数量边际，使反事实被抬高、超额被压低，因此是**保守下界**：只要它仍显著
    为正，凑单结论就不依赖“窗口外可识别”这类更强假设。

    ``composition_excluded``（窗口外边际）：只用研究窗口外的格估计价格与数量边际，窗口内
    格不参与拟合，因此不会吸收凑单，但反事实偏高、给出**上界**方向。若某个价格或数量
    在窗口外完全没有观测，其效应不可识别，本口径返回 None 并说明原因，不硬给数字。
    """
    total = len(records)
    if use_outside:
        prices = sorted({record["price_c"] for record in records})
        quantities = sorted({record["qty"] for record in records})
        fitting = [record for record in records if not exclusion_lo <= record["amount_c"] < exclusion_hi]
        price_counts, qty_counts = _composition_marginals(fitting)
        missing_prices = [price for price in prices if price_counts.get(price, 0) == 0]
        missing_quantities = [qty for qty in quantities if qty_counts.get(qty, 0) == 0]
        kind, role, source = "composition_excluded", "upper_bound", "窗口外格"
        if missing_prices or missing_quantities:
            detail = []
            if missing_prices:
                detail.append("价格 " + "、".join(_money(price) for price in missing_prices))
            if missing_quantities:
                detail.append("数量 " + "、".join(str(qty) for qty in missing_quantities))
            return None, {"fit": kind, "role": role, "marginals_from": source,
                          "warning": "窗口外没有观测到" + "、".join(detail) + "，该商品格效应不可识别"}
    else:
        fitting = list(records)
        price_counts, qty_counts = _composition_marginals(fitting)
        kind, role, source = "composition", "lower_bound", "全样本"
    expected = _independent_prediction(price_counts, qty_counts, total)
    if expected is None:
        return None, {"fit": kind, "role": role, "marginals_from": source, "warning": "边际样本不足"}
    return _calibrate(expected, total), {
        "fit": kind,
        "role": role,
        "marginals_from": source,
        "fitting_records": len(fitting),
        "price_share": {_money(price): _number(count / max(sum(price_counts.values()), 1), 4)
                        for price, count in sorted(price_counts.items())},
        "quantity_share": {str(qty): _number(count / max(sum(qty_counts.values()), 1), 4)
                           for qty, count in sorted(qty_counts.items())},
        "warning": None,
    }


def _fit_poisson(x, y, degree):
    design = np.vander(x, degree + 1, increasing=True)
    beta = np.zeros(design.shape[1])
    beta[0] = math.log(max(float(np.mean(y)), 1e-6))
    for _ in range(300):
        eta = np.clip(design @ beta, -30.0, 30.0)
        mu = np.exp(eta)
        weight = np.maximum(mu, 1e-8)
        working = eta + (y - mu) / weight
        matrix = (design.T * weight) @ design + 1e-8 * np.eye(design.shape[1])
        try:
            candidates = np.linalg.solve(matrix, (design.T * weight) @ working)
        except np.linalg.LinAlgError:
            candidates = np.linalg.lstsq(matrix, (design.T * weight) @ working, rcond=None)[0]
        if not np.all(np.isfinite(candidates)):
            return None
        if np.max(np.abs(candidates - beta)) < 1e-9:
            return candidates
        beta = candidates
    return beta


def _poisson(records, attainable, exclusion_lo, exclusion_hi, degree):
    observed = Counter(record["amount_c"] for record in records)
    x_all = np.asarray(attainable, dtype=float) / 100.0
    y_all = np.asarray([observed.get(amount, 0) for amount in attainable], dtype=float)
    keep = np.asarray([not exclusion_lo <= amount < exclusion_hi for amount in attainable])
    info = {"fit": f"poisson{degree}", "role": "smooth_descriptive", "outside_cells": int(keep.sum())}
    if keep.sum() < degree + 4:
        return None, {**info, "warning": "排除带外金额点不足"}
    x = x_all[keep]
    mean = float(x.mean())
    spread = float(x.std()) or 1.0
    beta = _fit_poisson((x - mean) / spread, y_all[keep], degree)
    if beta is None:
        return None, {**info, "warning": "平滑拟合未收敛"}
    design = np.vander((x_all - mean) / spread, degree + 1, increasing=True)
    fitted = np.exp(np.clip(design @ beta, -30.0, 30.0))
    expected = {amount: float(value) for amount, value in zip(attainable, fitted)}
    warning = "平滑模型在刻度上给出异常大值，仅作对照" if float(np.max(fitted, initial=0.0)) > max(
        8.0 * len(records), 400.0) else None
    return expected, {**info, "warning": warning}


def _counterfactual(model, records, attainable, exclusion_lo, exclusion_hi):
    if model in LATTICE_MODELS:
        return _composition(records, exclusion_lo, exclusion_hi, model == "composition_excluded")
    if model in SMOOTH_MODELS:
        return _poisson(records, attainable, exclusion_lo, exclusion_hi, int(model[-1]))
    raise StudyError("未知拟合选择：" + str(model))


# ---------------------------------------------------------------------------
# 单规格聚集量估计


def _windows(threshold_c, window_c, attainable):
    return ([amount for amount in attainable if threshold_c - window_c <= amount < threshold_c],
            [amount for amount in attainable if threshold_c <= amount < threshold_c + window_c])


def _estimate(records, threshold_c, window, exclusion, model, attainable=None):
    window_c = int(round(window * 100))
    exclusion_c = int(round(exclusion * 100))
    attainable = attainable or _attainable(records)
    observed = Counter(record["amount_c"] for record in records)
    below, above = _windows(threshold_c, window_c, attainable)
    expected, info = _counterfactual(model, records, attainable,
                                     threshold_c - exclusion_c, threshold_c + exclusion_c)
    if expected is None:
        return None
    sides = {}
    for name, amounts in (("below", below), ("above", above)):
        observed_count = sum(observed.get(amount, 0) for amount in amounts)
        expected_count = sum(expected.get(amount, 0.0) for amount in amounts)
        excess = observed_count - expected_count
        standard = math.sqrt(max(observed_count + expected_count, 0.0))
        sides[name] = {
            "observed": observed_count, "expected": _number(expected_count, 3),
            "excess": _number(excess, 3),
            "excess_interval_95": [_number(excess - 1.96 * standard, 3),
                                   _number(excess + 1.96 * standard, 3)],
            "p_greater": _number(_upper_p(observed_count, expected_count), 6),
            "cells": [{"amount": _money(amount), "observed": observed.get(amount, 0),
                       "expected": _number(expected.get(amount, 0.0), 3),
                       "excess": _number(observed.get(amount, 0) - expected.get(amount, 0.0), 3)}
                      for amount in amounts],
        }
    return {"threshold": _money(threshold_c), "window": window, "exclusion": exclusion, "model": model,
            "below": sides["below"], "above": sides["above"], "diagnostics": info}


def _specification_curve(records, threshold_c):
    specs = []
    attainable = _attainable(records)
    for window in WINDOW_GRID:
        for exclusion in EXCLUSION_GRID:
            if exclusion < window:
                continue
            for model in MODELS:
                estimate = _estimate(records, threshold_c, window, exclusion, model, attainable)
                if estimate is None:
                    specs.append({"window": window, "exclusion": exclusion, "model": model,
                                  "role": "unavailable", "below_excess": None, "above_excess": None,
                                  "warning": "该规格不可识别或无法拟合"})
                    continue
                specs.append({
                    "window": window, "exclusion": exclusion, "model": model,
                    "role": estimate["diagnostics"].get("role", "smooth_descriptive"),
                    "below_observed": estimate["below"]["observed"],
                    "below_expected": estimate["below"]["expected"],
                    "below_excess": estimate["below"]["excess"],
                    "below_p_greater": estimate["below"]["p_greater"],
                    "above_observed": estimate["above"]["observed"],
                    "above_expected": estimate["above"]["expected"],
                    "above_excess": estimate["above"]["excess"],
                    "warning": estimate["diagnostics"].get("warning"),
                })
    return specs


def _summarize(specs, key, roles, include_warned=False):
    usable = [spec for spec in specs if spec.get(key) is not None and spec.get("role") in roles
              and (include_warned or not spec.get("warning"))]
    values = [spec[key] for spec in usable]
    if not values:
        return {"count": 0, "minimum": None, "median": None, "maximum": None,
                "share_positive": None, "sign_stable": False, "roles": list(roles), "warnings": []}
    return {
        "count": len(values), "minimum": _number(min(values), 3), "median": _number(_median(values), 3),
        "maximum": _number(max(values), 3),
        "share_positive": _number(sum(1 for value in values if value > 0) / len(values), 4),
        "sign_stable": all(value > 0 for value in values) or all(value < 0 for value in values),
        "roles": list(roles),
        "warnings": sorted({spec["warning"] for spec in usable if spec.get("warning")}),
    }


def _bracket(specs):
    """刻度模型给出的区间：保守下界族（全样本边际）与窗口外上界族。"""
    lower = _summarize(specs, "below_excess", ("lower_bound",))
    upper = _summarize(specs, "below_excess", ("upper_bound",))
    smooth = _summarize(specs, "below_excess", ("smooth_descriptive",))
    bracket = None
    if lower["count"] and upper["count"]:
        bracket = {"lower_minimum": lower["minimum"], "upper_maximum": upper["maximum"],
                   "lower_median": lower["median"], "upper_median": upper["median"],
                   "contains_zero": lower["minimum"] <= 0 <= upper["maximum"]}
    return {"conservative_lower_bound": lower, "outside_upper_bound": upper,
            "smooth_descriptive": smooth, "bracket": bracket}


# ---------------------------------------------------------------------------
# 规则外虚假门槛


def _placebo_centers(threshold_c, exclusion_c, attainable, other_thresholds_c):
    centers = [(amount, "observed_lattice") for amount in attainable]
    for left, right in zip(attainable, attainable[1:]):
        midpoint = (left + right) // 2
        if midpoint not in attainable:
            centers.append((midpoint, "midpoint"))
    return [(center, kind) for center, kind in sorted(centers)
            if abs(center - threshold_c) > exclusion_c
            and all(abs(center - other) > exclusion_c for other in other_thresholds_c)]


def _placebo(records, threshold_c, window, exclusion, model, other_thresholds_c, attainable):
    entries = []
    for center, kind in _placebo_centers(threshold_c, int(round(exclusion * 100)),
                                         attainable, other_thresholds_c):
        estimate = _estimate(records, center, window, exclusion, model, attainable)
        if estimate is None:
            continue
        entries.append({
            "center": _money(center), "kind": kind,
            "below_observed": estimate["below"]["observed"],
            "below_expected": estimate["below"]["expected"],
            "below_excess": estimate["below"]["excess"],
            "above_observed": estimate["above"]["observed"],
            "above_expected": estimate["above"]["expected"],
            "above_excess": estimate["above"]["excess"],
        })
    return entries


def _placebo_summary(values):
    if not values:
        return {"count": 0, "minimum": None, "p05": None, "median": None, "p95": None, "maximum": None}
    return {"count": len(values), "minimum": _number(min(values), 3),
            "p05": _number(_quantile(values, 0.05), 3), "median": _number(_median(values), 3),
            "p95": _number(_quantile(values, 0.95), 3), "maximum": _number(max(values), 3)}


# ---------------------------------------------------------------------------
# 留出区间、跨门槛期留出反事实与跨期对照


def _within_window_fit(records, threshold_c, window, exclusion, model):
    estimate = _estimate(records, threshold_c, window, exclusion, model)
    if estimate is None:
        return None
    cells = estimate["below"]["cells"] + estimate["above"]["cells"]
    statistic = 0.0
    usable = 0
    details = []
    for cell in cells:
        expected = cell["expected"] or 0.0
        contribution = (cell["observed"] - expected) ** 2 / expected if expected > 0 else 0.0
        statistic += contribution
        usable += 1 if expected > 0 else 0
        details.append({**cell, "residual": cell["excess"], "chi_square": _number(contribution, 3)})
    return {
        "model": model, "cells": details, "chi_square": _number(statistic, 3), "usable_cells": usable,
        "p_value": _number(sps.chi2.sf(statistic, usable), 6) if usable else None,
        "total_excess": _number(estimate["below"]["excess"] + estimate["above"]["excess"], 3),
        "note": ("门槛带内逐点残差，描述反事实在留出区间上的拟合优度；主反事实若使用全样本"
                 "边际则仍在样本内，不能单独作为凑单证据。"),
    }


def _transfer(target_records, control_records, control_thresholds_c, threshold_c, window,
              period_window):
    """跨门槛期留出反事实：用另一门槛期的商品构成预测本期门槛带。

    对照期的商品价格×数量边际来自它自己的样本，并先剔除对照期各门槛的 ±period_window
    带，避免把对照期自己的凑单带进边际。本期门槛带完全不参与拟合，是真正不带本期门槛
    信息的反事实；反事实总量按本期订单数缩放。

    覆盖限制：只在两期商品目录都可达到的金额点上比较。对照期不可能产生的金额点记为
    ``uncovered``，不参与检验也不当作超额——那更可能反映商品目录差异而非凑单。若门槛
    下方没有任何可覆盖金额点，返回 ``available: False``。
    """
    if not control_records:
        return None
    window_c = int(round(window * 100))
    period_c = int(round(period_window * 100))
    fitting = [record for record in control_records
               if all(abs(record["amount_c"] - other) > period_c for other in control_thresholds_c)]
    if len(fitting) < 30:
        return {"available": False, "control_orders": len(fitting),
                "reason": "对照期剔除门槛带后订单过少，不能做跨期留出反事实"}
    price_counts, qty_counts = _composition_marginals(fitting)
    attainable = {price * qty for price in price_counts for qty in qty_counts}
    expected_full = _independent_prediction(price_counts, qty_counts, len(target_records))
    target_attainable = _attainable(target_records)
    observed = Counter(record["amount_c"] for record in target_records)
    sides = {}
    for name, amounts in zip(("below", "above"), _windows(threshold_c, window_c, target_attainable)):
        covered = [amount for amount in amounts if amount in attainable]
        uncovered = [amount for amount in amounts if amount not in attainable]
        observed_count = sum(observed.get(amount, 0) for amount in covered)
        expected_count = sum(expected_full.get(amount, 0.0) for amount in covered)
        uncovered_observed = sum(observed.get(amount, 0) for amount in uncovered)
        sides[name] = {
            "covered_amounts": [_money(amount) for amount in covered],
            "uncovered_amounts": [_money(amount) for amount in uncovered],
            "uncovered_observed": uncovered_observed,
            "observed": observed_count, "expected": _number(expected_count, 3),
            "excess": _number(observed_count - expected_count, 3),
            "p_greater": _number(_upper_p(observed_count, expected_count), 6),
            "cells": [{"amount": _money(amount), "observed": observed.get(amount, 0),
                       "expected": _number(expected_full.get(amount, 0.0), 3),
                       "excess": _number(observed.get(amount, 0) - expected_full.get(amount, 0.0), 3)}
                      for amount in covered],
        }
    availability = "available" if sides["below"]["covered_amounts"] else "unavailable"
    result = {
        "available": availability == "available",
        "control_orders": len(fitting), "target_orders": len(target_records),
        "control_excluded_band": period_window,
        "below": sides["below"], "above": sides["above"],
        "note": ("用另一门槛期（剔除其门槛带后）的商品边际预测本期门槛带；本期门槛两侧都不参与"
                 "拟合。对照期不可达到的金额点不参与比较，并单列其观察数，避免把商品目录差异"
                 "当成凑单或当成无堆积。"),
    }
    if availability != "available":
        result["reason"] = "门槛下方没有两期都可达到的金额点，无法做跨期留出反事实"
    return result


def _regime_contrast(records, threshold_c, window_c, control_records, control_thresholds_c):
    if not control_records:
        return None
    attainable = _attainable(records)
    counts = Counter(record["amount_c"] for record in records)
    control_counts = Counter(record["amount_c"] for record in control_records)
    target_total = len(records)
    control_total = len(control_records)
    cells = []
    for amount in attainable:
        if abs(amount - threshold_c) >= window_c:
            continue
        placement = "below" if amount < threshold_c else "above"
        target_count = counts.get(amount, 0)
        control_count = control_counts.get(amount, 0)
        p_target, p_control = _share(target_count, target_total), _share(control_count, control_total)
        variance = p_target * (1 - p_target) / target_total + p_control * (1 - p_control) / control_total
        standard = math.sqrt(variance) if variance > 0 else None
        difference = p_target - p_control
        z = difference / standard if standard else None
        distance = min((abs(amount - other) for other in control_thresholds_c), default=None)
        cells.append({
            "amount": _money(amount), "side": placement,
            "target_count": target_count, "target_share": _number(p_target, 5),
            "control_count": control_count, "control_share": _number(p_control, 5),
            "difference": _number(difference, 5), "z": _number(z, 3),
            "p_greater": _number(float(sps.norm.sf(z)), 6) if z is not None else None,
            "distance_to_control_threshold": _money(distance) if distance is not None else None,
            "clean_control": distance is not None and distance >= window_c,
        })
    clean_below = [cell for cell in cells if cell["side"] == "below" and cell["clean_control"]]
    combined = None
    if clean_below:
        target_count = sum(cell["target_count"] for cell in clean_below)
        control_count = sum(cell["control_count"] for cell in clean_below)
        p_target, p_control = _share(target_count, target_total), _share(control_count, control_total)
        variance = p_target * (1 - p_target) / target_total + p_control * (1 - p_control) / control_total
        standard = math.sqrt(variance) if variance > 0 else None
        difference = p_target - p_control
        z = difference / standard if standard else None
        upper = difference + _normal_quantile(0.95) * standard if standard else None
        combined = {
            "amounts": [cell["amount"] for cell in clean_below],
            "target_count": target_count, "control_count": control_count,
            "target_share": _number(p_target, 5), "control_share": _number(p_control, 5),
            "difference": _number(difference, 5), "z": _number(z, 3),
            "p_greater": _number(float(sps.norm.sf(z)), 6) if z is not None else None,
            "upper_bound_share_95": _number(upper, 5),
            "upper_bound_orders_95": _number(upper * target_total, 2) if upper is not None else None,
            "detectable_orders_95": _number(_normal_quantile(0.95) * standard * target_total, 2)
            if standard else None,
        }
    return {
        "control_thresholds": [_money(value) for value in sorted(control_thresholds_c)],
        "target_orders": target_total, "control_orders": control_total,
        "clean_definition": "金额点与对照期所有门槛的距离都不小于研究窗口",
        "cells": cells, "combined_clean_below": combined,
        "note": ("同一金额刻度若在另一门槛期（当时该处不是门槛）同样密集，则门槛附近自然密集与"
                 "凑单在观察数据上无法区分；只有“干净对照”的金额点参与合并上界。"),
    }


# ---------------------------------------------------------------------------
# 经营核对


def _net(order, rule, settlement, threshold_c):
    amount = _cents(order["qualifying_amount"])
    fee = _cents(rule["shipping_fee"]) if amount < threshold_c else 0
    coupon = _cents(order["delivery_coupon"])
    refund = _cents(settlement.get("refund", "0"))
    product = _cents(settlement.get("product_cost", "0"))
    fulfillment = _cents(settlement.get("fulfillment_cost", "0"))
    # 净收入 = 资格金额 + 实收运费 - 配送券 - 退款 - 商品成本 - 履约成本。
    # 退款只冲减净收入，不回写资格金额；履约成本单列，便于区分“新增订单”与“改写购物篮”。
    net_after_coupon = amount + fee - coupon - refund - product - fulfillment
    return {"amount": amount, "fee": fee, "coupon": coupon, "refund": refund, "product": product,
            "fulfillment": fulfillment, "net_before_coupon": net_after_coupon + coupon,
            "net_after_coupon": net_after_coupon}


def _money_range(values):
    if not values:
        return {"minimum": None, "median": None, "maximum": None}
    return {"minimum": _money(min(values)), "median": _money(int(round(_median(values)))),
            "maximum": _money(max(values))}


def _economics(orders, rules, settlements, threshold_c, window_c):
    threshold_by_rule = {rule_id: _cents(rule["threshold"]) for rule_id, rule in rules.items()}
    selected = [order for order in orders
                if threshold_c - window_c <= _cents(order["qualifying_amount"]) < threshold_c + window_c]
    net = [_net(order, rules[order["rule_id"]], settlements.get(order["id"], {}),
                threshold_by_rule[order["rule_id"]]) for order in selected]
    gross = sum(item["amount"] for item in net)
    return {
        "orders": len(selected),
        "below_orders": sum(1 for item in net if item["amount"] < threshold_c),
        "above_orders": sum(1 for item in net if item["amount"] >= threshold_c),
        "gross_qualifying_amount": _money(gross),
        "shipping_fee_charged": _money(sum(item["fee"] for item in net)),
        "delivery_coupon": _money(sum(item["coupon"] for item in net)),
        "refund": _money(sum(item["refund"] for item in net)),
        "product_cost": _money(sum(item["product"] for item in net)),
        "fulfillment_cost": _money(sum(item["fulfillment"] for item in net)),
        "net_revenue_before_coupon": _money(sum(item["net_before_coupon"] for item in net)),
        "net_revenue_after_coupon": _money(sum(item["net_after_coupon"] for item in net)),
        "net_revenue_after_coupon_per_order": _money_range([item["net_after_coupon"] for item in net]),
        "fulfillment_cost_per_order": _money_range([item["fulfillment"] for item in net]),
        "contribution_ratio_before_coupon": _number(
            sum(item["net_before_coupon"] for item in net) / max(gross, 1), 4),
        "fill_in_gap_per_order": _money_range(
            [threshold_c - item["amount"] for item in net if item["amount"] < threshold_c]),
        "interpretation": ("观察到的窗口订单净收入与履约成本，不是凑单增量；窗口内的订单本来也会"
                           "发生。把整单净收入乘上“超额订单数”会把已有订单记成新增销售。"),
    }


# ---------------------------------------------------------------------------
# 单个门槛组


def _prepare(loaded, rule_ids):
    orders = [order for order in loaded["orders"] if order["rule_id"] in rule_ids]
    completed = [order for order in orders if not order["cancelled"]]
    records = []
    excluded_multi = []
    for order in completed:
        lines = loaded["lines_by_order"].get(order["id"], [])
        if len(lines) != 1:
            excluded_multi.append(order["id"])
            continue
        line = lines[0]
        records.append({"order": order["id"], "amount_c": _cents(order["qualifying_amount"]),
                        "price_c": _cents(line["unit_price"]), "qty": int(line["quantity"])})
    return orders, completed, records, excluded_multi


def _empty_group(loaded, threshold_c, rule_ids, orders, completed, records, excluded_multi):
    rules = loaded["rules"]
    return {
        "threshold": _money(threshold_c),
        "rules": [{"id": rule["id"], "region": rule["region"], "valid_from": rule["valid_from"],
                   "valid_to": rule["valid_to"], "amount_basis": rule["amount_basis"],
                   "shipping_fee": _money(_cents(rule["shipping_fee"]))}
                  for rule in sorted((rules[rule_id] for rule_id in rule_ids), key=lambda item: item["id"])],
        "sample": {"orders": len(orders), "completed": len(completed),
                   "cancelled_excluded": len(orders) - len(completed),
                   "single_line_records": len(records), "multi_line_excluded": len(excluded_multi),
                   "regions": sorted({order["region"] for order in orders}),
                   "first_day": min((order["created_at"][:10] for order in orders), default=None),
                   "last_day": max((order["created_at"][:10] for order in orders), default=None),
                   "frequency_unit": "每单一次，不按商品行扩张"},
        "identification": {
            "status": "insufficient",
            "reasons": [{"code": "insufficient_records",
                         "detail": "该门槛下可用于刻度反事实的单行成交订单不足，无法估计超额"}],
            "headline": "识别不足：资料不足以在门槛附近构造反事实，不给出凑单量或收益估计。",
            "point_estimate_orders": None, "specification_range_orders": [None, None],
            "upper_bound_orders_95": None,
            "incremental_orders_identified": False,
        },
        "economics": {"orders": len(completed), "translation": None,
                      "interpretation": "资料不足，不做经营换算。"},
    }


def _group(loaded, threshold_c, rule_ids, window, exclusion, model, other_thresholds_c,
           control_records, control_thresholds_c):
    rules = loaded["rules"]
    orders, completed, records, excluded_multi = _prepare(loaded, rule_ids)
    window_c = int(round(window * 100))
    exclusion_c = int(round(exclusion * 100))
    attainable = _attainable(records)
    default = _estimate(records, threshold_c, window, exclusion, model, attainable)
    if default is None:
        return _empty_group(loaded, threshold_c, rule_ids, orders, completed, records, excluded_multi)
    specs = _specification_curve(records, threshold_c)
    families = _bracket(specs)
    placebos = _placebo(records, threshold_c, window, exclusion, model, other_thresholds_c, attainable)
    placebo_below = [entry["below_excess"] for entry in placebos if entry["below_excess"] is not None]
    within_fit = _within_window_fit(records, threshold_c, window, exclusion, model)
    transfer = _transfer(records, control_records, other_thresholds_c, threshold_c, window, window)
    contrast = _regime_contrast(records, threshold_c, window_c, control_records, other_thresholds_c)
    economics = _economics(completed, rules, loaded["settlements"], threshold_c, window_c)

    coupon_crossings = sum(
        1 for order in completed
        if (_cents(order["qualifying_amount"]) >= threshold_c)
        != (_cents(order["qualifying_amount"]) - _cents(order["delivery_coupon"]) >= threshold_c))
    refund_crossings = sum(
        1 for order in completed
        if (_cents(order["qualifying_amount"]) >= threshold_c)
        != (_cents(order["qualifying_amount"])
            - _cents(loaded["settlements"].get(order["id"], {}).get("refund", "0")) >= threshold_c))

    conservative = families["conservative_lower_bound"]
    real_below = default["below"]["excess"]
    real_above = default["above"]["excess"]
    placebo_rank = None
    if placebo_below:
        placebo_rank = _number(sum(1 for value in placebo_below if value <= real_below)
                               / len(placebo_below), 4)
    below_points = [amount for amount in _windows(threshold_c, window_c, attainable)[0]
                    if any(record["amount_c"] == amount for record in records)]
    above_points = [amount for amount in _windows(threshold_c, window_c, attainable)[1]
                    if any(record["amount_c"] == amount for record in records)]
    basis_variants = {rule["amount_basis"] for rule in rules.values() if rule["id"] in rule_ids}

    reasons = []
    # 1. 保守下界必须显著为正
    if default["below"]["p_greater"] is None or default["below"]["p_greater"] > SIGNIFICANCE:
        reasons.append({"code": "conservative_excess_not_significant",
                        "detail": "保守下限（全样本边际的构成模型）下门槛下方超额不显著为正"})
    # 2. 保守族在窗口／排除区间下符号必须稳定
    if not conservative["sign_stable"]:
        reasons.append({"code": "specification_unstable",
                        "detail": "窗口与排除区间变化时，保守下限的超额符号不稳定"})
    # 3. 真实门槛的超额不能落在规则外虚假门槛的中段
    if placebo_rank is not None and PLACEBO_MIDDLE[0] <= placebo_rank <= PLACEBO_MIDDLE[1]:
        reasons.append({"code": "placebo_indistinguishable",
                        "detail": "规则外虚假门槛的超额分布覆盖真实门槛的超额，无法区分"})
    # 4. 另一门槛期同一刻度不能同样密集
    if contrast and contrast["combined_clean_below"]:
        combined = contrast["combined_clean_below"]
        if combined["p_greater"] is None or combined["p_greater"] > SIGNIFICANCE:
            reasons.append({"code": "natural_pileup_cross_regime",
                            "detail": "门槛下方同一金额刻度在另一门槛期同样密集，自然堆积无法与凑单区分"})
    # 5. 跨门槛期留出反事实必须也显示超额
    if transfer and transfer.get("available"):
        p_value = transfer["below"]["p_greater"]
        if p_value is None or p_value > SIGNIFICANCE:
            reasons.append({"code": "held_out_counterfactual_no_excess",
                            "detail": "用另一门槛期商品边际做留出反事实时，本期门槛下方超额不显著"})
    elif transfer and not transfer.get("available"):
        reasons.append({"code": "held_out_counterfactual_unavailable", "detail": transfer["reason"]})
    else:
        reasons.append({"code": "no_control_regime",
                        "detail": "资料中没有可用的另一门槛期作对照，无法区分自然堆积与凑单"})
    if len(below_points) < 2:
        reasons.append({"code": "below_support_narrow",
                        "detail": "门槛下方研究窗口内实际出现的金额点少于两个，窗口选择决定结论"})
    if not above_points:
        reasons.append({"code": "above_support_missing",
                        "detail": "门槛上方研究窗口内没有可观测金额点，无法估计跨门槛聚集"})
    if not placebos:
        reasons.append({"code": "placebo_missing", "detail": "没有可用的规则外对照金额点"})
    if len(basis_variants) > 1:
        reasons.append({"code": "mixed_amount_basis", "detail": "同一门槛的规则使用了不同金额口径，不可合并"})
    status = "insufficient" if reasons else "supported"
    identified = status == "supported"

    # 经营换算：只给“最多可能有多少单”的上界，以及观察到的每单净收入与履约成本。
    upper_bound = None
    if contrast and contrast["combined_clean_below"]:
        upper_bound = contrast["combined_clean_below"]["upper_bound_orders_95"]
    interval = default["below"]["excess_interval_95"]
    translation = None
    if interval[1] is not None:
        bound = max(0.0, float(interval[1]))
        if upper_bound is not None:
            bound = max(bound, float(upper_bound))
        per_order_net = economics["net_revenue_after_coupon_per_order"]
        per_order_fulfillment = economics["fulfillment_cost_per_order"]
        fulfillment = _cents(per_order_fulfillment["maximum"] or "0")
        translation = {
            "orders_upper_bound_95": _number(bound, 2),
            "bound_source": "跨门槛期干净对照上界与首选反事实区间上端的较大者",
            "observed_net_revenue_per_order": per_order_net,
            "observed_fulfillment_cost_per_order": per_order_fulfillment,
            "fulfillment_cost_if_every_excess_order_is_new": _money(int(round(bound * fulfillment))),
            "fulfillment_cost_if_baskets_merely_modified": "0.00",
            "incremental_orders_identified": identified,
            "incremental_sales_identified": False,
            "multiplying_window_net_revenue_overstates_new_sales": True,
            "note": ("上限只约束“最多可能有多少单”，不是凑单量。窗口订单的净收入与履约成本按观察值"
                     "给出；把整单净收入乘上该上限会把本来就会发生的订单记成新增销售。凑单若只是"
                     "改写已有购物篮，新增收入只可能来自多买的那部分商品，本资料无法识别。这不是"
                     "新增销售预测，也不构成收益承诺。"),
        }

    if identified:
        headline = ("在保守下限、规则外虚假门槛、跨门槛期留出反事实与规格曲线下仍显示同向显著超额；"
                    "仍不能据此区分新增订单与改写购物篮。")
    else:
        headline = ("识别不足：门槛附近的堆积与商品价格刻度的自然密集、另一门槛期同一刻度的密集"
                    "无法区分，不能据此认定凑单，也不给出收益承诺。")

    return {
        "threshold": _money(threshold_c),
        "rules": [{"id": rule["id"], "region": rule["region"], "valid_from": rule["valid_from"],
                   "valid_to": rule["valid_to"], "amount_basis": rule["amount_basis"],
                   "shipping_fee": _money(_cents(rule["shipping_fee"]))}
                  for rule in sorted((rules[rule_id] for rule_id in rule_ids), key=lambda item: item["id"])],
        "sample": {
            "orders": len(orders), "completed": len(completed),
            "cancelled_excluded": len(orders) - len(completed),
            "single_line_records": len(records), "multi_line_excluded": len(excluded_multi),
            "regions": sorted({order["region"] for order in orders}),
            "first_day": min(order["created_at"][:10] for order in orders),
            "last_day": max(order["created_at"][:10] for order in orders),
            "frequency_unit": "每单一次，不按商品行扩张",
        },
        "amount_scale": _scale([record["amount_c"] for record in records]),
        "lattice": {
            "attainable_amounts": [_money(amount) for amount in attainable],
            "observed_amounts": [_money(amount) for amount in sorted({r["amount_c"] for r in records})],
            "counts": {_money(amount): sum(1 for record in records if record["amount_c"] == amount)
                       for amount in attainable},
        },
        "price_structure": _price_structure(records, threshold_c - exclusion_c, threshold_c + exclusion_c),
        "running_variable": {
            "field": "qualifying_amount",
            "basis": "商品促销后、配送券前、事后退款前",
            "threshold_rule": "qualifying_amount 大于等于 threshold 即包邮",
            "threshold_lookup": "按订单 rule_id 锁定当时的门槛",
            "coupon_crossings_if_netted": coupon_crossings,
            "refund_crossings_if_netted": refund_crossings,
            "reclassified": False,
            "note": ("配送券与退款只影响净收入，不参与门槛归类。若把配送券从金额里扣掉，有 "
                     f"{coupon_crossings} 单会跨线；若把事后退款扣掉，有 {refund_crossings} 单会跨线。"
                     "本研究保留顾客确认时看到的金额。"),
        },
        "excess": default,
        "specification_curve": {
            "windows": list(WINDOW_GRID), "exclusions": list(EXCLUSION_GRID), "models": list(MODELS),
            "lattice_models": list(LATTICE_MODELS), "smooth_models": list(SMOOTH_MODELS),
            "point_estimate_orders": _number(real_below, 3),
            "point_estimate_role": "保守下限（全样本边际构成模型）",
            "families": families,
            "specs": specs,
        },
        "placebo": {
            "centers": placebos,
            "below_summary": _placebo_summary(placebo_below),
            "real_below_excess": _number(real_below, 3),
            "real_below_placebo_rank": placebo_rank,
            "real_above_excess": _number(real_above, 3),
            "above_summary": _placebo_summary(
                [entry["above_excess"] for entry in placebos if entry["above_excess"] is not None]),
        },
        "within_window_fit": within_fit,
        "held_out_counterfactual": transfer,
        "regime_contrast": contrast,
        "identification": {
            "status": status,
            "reasons": reasons,
            "natural_pileup": bool(contrast and contrast["combined_clean_below"] and
                                   (contrast["combined_clean_below"]["p_greater"] or 1.0) > SIGNIFICANCE),
            "point_estimate_orders": _number(real_below, 3),
            "point_estimate_interval_95": default["below"]["excess_interval_95"],
            "conservative_lower_range_orders": [conservative["minimum"], conservative["maximum"]],
            "outside_upper_range_orders": [families["outside_upper_bound"]["minimum"],
                                           families["outside_upper_bound"]["maximum"]],
            "placebo_rank": placebo_rank,
            "share_specs_positive": conservative["share_positive"],
            "held_out_excess_orders": transfer["below"]["excess"] if transfer and transfer.get("available") else None,
            "held_out_p_greater": transfer["below"]["p_greater"] if transfer and transfer.get("available") else None,
            "incremental_orders_identified": identified,
            "upper_bound_orders_95": _number(upper_bound, 2) if upper_bound is not None else None,
            "headline": headline,
        },
        "economics": {**economics, "translation": translation},
    }


# ---------------------------------------------------------------------------
# 对外入口


def _observed_thresholds(catalog):
    return sorted({_cents(rule["threshold"]) for rule in catalog.rows("shipping_rules")})


def study(catalog, threshold=None, window=DEFAULT_WINDOW, exclusion=DEFAULT_EXCLUSION,
          model=DEFAULT_MODEL):
    """执行门槛聚集研究。

    ``threshold`` 省略时研究资料中全部实际生效过的门槛。给定一个从未在
    ``shipping_rules`` 中生效的门槛时返回 ``out_of_support``，不给出任何收益估计。
    """
    if model not in MODELS:
        raise StudyError("拟合选择应为：" + "、".join(MODELS))
    try:
        window = float(window)
        exclusion = float(exclusion)
    except (TypeError, ValueError) as error:
        raise StudyError("窗口或排除区间不是有效数值") from error
    if not 0 < window <= 100:
        raise StudyError("研究窗口应在 0 到 100 元之间")
    if not window <= exclusion <= 200:
        raise StudyError("排除区间应不小于研究窗口且不超过 200 元")

    loaded = _load(catalog)
    integrity = _integrity(loaded)
    all_thresholds = _observed_thresholds(catalog)
    groups = defaultdict(list)
    for rule in loaded["rules"].values():
        groups[_cents(rule["threshold"])].append(rule["id"])

    if threshold is not None:
        try:
            requested = _cents(threshold)
        except Exception as error:  # noqa: BLE001
            raise StudyError("门槛金额格式无效") from error
        if requested not in all_thresholds:
            return {
                "status": "out_of_support",
                "source_fingerprint": getattr(catalog, "fingerprint", None),
                "requested_threshold": _money(requested),
                "observed_thresholds": [_money(value) for value in all_thresholds],
                "message": ("该门槛在 shipping_rules 中从未实际生效，资料中没有对应的运行变量断点；"
                            "不能估计其凑单、净收入或履约成本，也不给出任何收益承诺。"),
                "integrity": integrity,
            }
        selected = [requested]
    else:
        selected = all_thresholds

    records_by_threshold = {value: _prepare(loaded, groups[value])[2] for value in all_thresholds}
    results = []
    for value in selected:
        other = [item for item in all_thresholds if item != value]
        controls = [record for item, records in records_by_threshold.items()
                    if item != value for record in records]
        results.append(_group(loaded, value, groups[value], window, exclusion, model,
                              other, controls, other))

    return {
        "status": "ok" if results else "no_data",
        "source_fingerprint": getattr(catalog, "fingerprint", None),
        "parameters": {"window": window, "exclusion": exclusion, "model": model,
                       "models": list(MODELS), "windows": list(WINDOW_GRID),
                       "exclusions": list(EXCLUSION_GRID), "significance": SIGNIFICANCE,
                       "cancelled_orders": "默认排除，单独计数"},
        "integrity": integrity,
        "observed_thresholds": [_money(value) for value in all_thresholds],
        "unobserved_threshold_note": ("只有 shipping_rules 中实际生效过的门槛才有运行变量断点；"
                                      "对从未生效的门槛（例如更低的门槛）不做估计、不给收益承诺。"),
        "thresholds": results,
    }
