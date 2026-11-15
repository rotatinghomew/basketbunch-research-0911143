import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from basketbunch import bunching
from basketbunch.catalog import Catalog


PINNED = {
    "id": "text", "created_at": "time", "region": "text", "rule_id": "text",
    "qualifying_amount": "money", "delivery_coupon": "money", "cancelled": "bool",
}


def make_dataset(directory, rules, orders, lines=None, settlements=None):
    """写一套最小资料；金额用字符串，订单按 (id, region, rule, amount, coupon, cancelled)。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    schema = {
        "shipping_rules": {"key": "id", "columns": {
            "id": "text", "region": "text", "valid_from": "date", "valid_to": "date",
            "threshold": "money", "amount_basis": "text", "shipping_fee": "money"}, "references": {}},
        "orders": {"key": "id", "columns": PINNED, "references": {"rule_id": "shipping_rules.id"}},
        "order_lines": {"key": "id", "columns": {
            "id": "text", "order_id": "text", "sku": "text", "quantity": "int",
            "unit_price": "money", "discount": "money"}, "references": {"order_id": "orders.id"}},
        "settlements": {"key": "id", "columns": {
            "id": "text", "order_id": "text", "day": "date", "refund": "money",
            "fulfillment_cost": "money", "product_cost": "money"}, "references": {"order_id": "orders.id"}},
    }
    (directory / "schema.json").write_text(json.dumps(schema), encoding="utf-8")

    def write(name, header, records):
        with (directory / (name + ".csv")).open("w", newline="", encoding="utf-8") as target:
            writer = csv.writer(target)
            writer.writerow(header)
            writer.writerows(records)

    write("shipping_rules",
          ["id", "region", "valid_from", "valid_to", "threshold", "amount_basis", "shipping_fee"], rules)
    order_rows = [[row["id"], row.get("created_at", "2026-05-01T12:00:00+08:00"), row["region"],
                   row["rule"], row["amount"], row.get("coupon", "0.00"), row.get("cancelled", "0")]
                  for row in orders]
    write("orders", ["id", "created_at", "region", "rule_id", "qualifying_amount", "delivery_coupon",
                     "cancelled"], order_rows)
    line_rows = lines or [[f"L{row['id']}", row["id"], f"SKU{row['amount']}", 1, row["amount"], "0.00"]
                          for row in orders]
    write("order_lines", ["id", "order_id", "sku", "quantity", "unit_price", "discount"], line_rows)
    settlement_rows = settlements or [[f"S{row['id']}", row["id"], "2026-06-01", "0.00", "10.00",
                                       f"{Decimal(row['amount']) * Decimal('0.6'):.2f}"]
                                      for row in orders]
    write("settlements", ["id", "order_id", "day", "refund", "fulfillment_cost", "product_cost"],
          settlement_rows)
    return Catalog(directory)


class BunchingTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)

    def dataset(self, rules, orders, **kwargs):
        return make_dataset(self.root / "data", rules, orders, **kwargs)

    def test_unobserved_threshold_returns_out_of_support(self):
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]],
            [{"id": "O1", "region": "片区0", "rule": "R1", "amount": "50.00"}])
        result = bunching.study(catalog, threshold="79")
        self.assertEqual(result["status"], "out_of_support")
        self.assertEqual(result["observed_thresholds"], ["100.00"])
        self.assertNotIn("economics", result)
        self.assertIn("收益承诺", result["message"])

    def test_invalid_parameters_rejected(self):
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]],
            [{"id": "O1", "region": "片区0", "rule": "R1", "amount": "50.00"}])
        with self.assertRaises(bunching.StudyError):
            bunching.study(catalog, model="linear")
        with self.assertRaises(bunching.StudyError):
            bunching.study(catalog, window=0)
        with self.assertRaises(bunching.StudyError):
            bunching.study(catalog, window=20, exclusion=5)
        with self.assertRaises(bunching.StudyError):
            bunching.study(catalog, window="wide")

    def test_amounts_are_cents_exact(self):
        self.assertEqual(bunching._cents("99.00"), 9900)
        self.assertEqual(bunching._cents("99.005"), 9901)
        self.assertEqual(bunching._money(9900), "99.00")
        self.assertEqual(bunching._money(1), "0.01")

    def test_bunching_detected_when_lattice_is_clean(self):
        # 商品目录有多条价格与数量组合，门槛 100 附近有两个金额点 96（32×3）与 99（33×3）。
        # 向这两点注入大量订单，并提供一个不含凑单的对照门槛期；所有识别检查都同向时报告
        # “有支持”。若拿掉对照期（见 test_no_control_regime_is_insufficient），必须报识别不足。
        prices = [10, 20, 25, 30, 32, 33, 40, 45, 50, 60, 70, 80, 90]
        orders = []
        lines = []

        def add(price, quantity, rule="R1", region="片区0", tag="O"):
            amount = price * quantity
            identifier = f"{tag}{len(orders)}"
            orders.append({"id": identifier, "region": region, "rule": rule,
                           "amount": f"{amount:.2f}"})
            lines.append([f"L{identifier}", identifier, "S", quantity, f"{price:.2f}", "0.00"])

        for step in range(1400):
            add(prices[step % len(prices)], (1, 2, 3)[step % 3])
        for _ in range(200):
            add(33, 3)
        for _ in range(150):
            add(32, 3)
        for step in range(1200):
            add(prices[step % len(prices)], (1, 2, 3)[step % 3], rule="R2", region="片区1", tag="C")
        rules = [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"],
                 ["R2", "片区1", "2026-01-01", "2026-12-31", "250", "商品促销后配送券前", "8.00"]]
        catalog = self.dataset(rules, orders, lines=lines)
        threshold = bunching.study(catalog, threshold="100")["thresholds"][0]
        self.assertGreater(threshold["excess"]["below"]["excess"], 100)
        self.assertEqual(threshold["identification"]["status"], "supported")
        self.assertEqual(threshold["identification"]["reasons"], [])

    def test_no_control_regime_is_insufficient(self):
        # 只有真实门槛、没有另一门槛期的对照时，无法排除“商品刻度自然密集”，必须报识别不足，
        # 而不是直接认定凑单。
        orders = [{"id": f"O{i}", "region": "片区0", "rule": "R1", "amount": "99.00"}
                  for i in range(300)]
        rules = [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]]
        catalog = self.dataset(rules, orders)
        result = bunching.study(catalog, threshold="100")["thresholds"][0]
        codes = {reason["code"] for reason in result["identification"]["reasons"]}
        self.assertIn("no_control_regime", codes)
        self.assertEqual(result["identification"]["status"], "insufficient")
        self.assertFalse(result["identification"]["incremental_orders_identified"])

    def test_smooth_omitted_variable_reports_insufficient(self):
        # 单价都是 10 的整数倍，金额 = 10 的倍数；门槛 95 落在两个刻度之间，
        # 此时“门槛下方”窗口里只有一个点，窗口选择决定结论，应报识别不足。
        orders = []
        for index in range(120):
            amount = 10 * (1 + index % 9)
            orders.append({"id": f"O{index}", "region": "片区0", "rule": "R1",
                           "amount": f"{amount}.00"})
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "95", "商品促销后配送券前", "8.00"]],
            orders)
        result = bunching.study(catalog, threshold="95")
        codes = {reason["code"] for reason in result["thresholds"][0]["identification"]["reasons"]}
        self.assertIn("below_support_narrow", codes)
        self.assertEqual(result["thresholds"][0]["identification"]["status"], "insufficient")

    def test_refund_and_coupon_do_not_reclassify(self):
        orders = [{"id": "O1", "region": "片区0", "rule": "R1", "amount": "98.00", "coupon": "5.00"}]
        settlements = [["S1", "O1", "2026-06-01", "15.00", "10.00", "60.00"]]
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]],
            orders, settlements=settlements)
        result = bunching.study(catalog, threshold="100")
        running = result["thresholds"][0]["running_variable"]
        self.assertFalse(running["reclassified"])
        self.assertEqual(running["coupon_crossings_if_netted"], 0)
        self.assertEqual(running["refund_crossings_if_netted"], 0)
        # 金额仍是顾客看到的 98，退款只降低净收入
        self.assertEqual(result["thresholds"][0]["economics"]["gross_qualifying_amount"], "98.00")
        # 98 + 包邮省下 8 运费 - 5 配送券 - 15 退款 - 60 商品成本 - 10 履约 = 16
        self.assertEqual(result["thresholds"][0]["economics"]["net_revenue_after_coupon"], "16.00")

    def test_coupon_crossing_is_counted_but_not_reclassified(self):
        orders = [{"id": "O1", "region": "片区0", "rule": "R1", "amount": "101.00", "coupon": "5.00"}]
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]],
            orders)
        running = bunching.study(catalog, threshold="100")["thresholds"][0]["running_variable"]
        self.assertEqual(running["coupon_crossings_if_netted"], 1)
        self.assertFalse(running["reclassified"])

    def test_cancelled_orders_reported_not_used(self):
        orders = [{"id": "O1", "region": "片区0", "rule": "R1", "amount": "99.00"},
                  {"id": "O2", "region": "片区0", "rule": "R1", "amount": "99.00", "cancelled": "1"}]
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]],
            orders)
        sample = bunching.study(catalog, threshold="100")["thresholds"][0]["sample"]
        self.assertEqual(sample["completed"], 1)
        self.assertEqual(sample["cancelled_excluded"], 1)

    def test_thresholds_use_rule_not_latest(self):
        rules = [
            ["R-old", "片区0", "2026-01-01", "2026-04-01", "99", "商品促销后配送券前", "8.00"],
            ["R-new", "片区0", "2026-04-01", "2026-07-01", "79", "商品促销后配送券前", "8.00"],
        ]
        orders = [{"id": "O1", "region": "片区0", "rule": "R-old", "amount": "95.00",
                   "created_at": "2026-02-01T12:00:00+08:00"},
                  {"id": "O2", "region": "片区0", "rule": "R-new", "amount": "85.00",
                   "created_at": "2026-05-01T12:00:00+08:00"}]
        catalog = self.dataset(rules, orders)
        result = bunching.study(catalog)
        self.assertEqual(result["observed_thresholds"], ["79.00", "99.00"])
        by_threshold = {item["threshold"]: item for item in result["thresholds"]}
        self.assertEqual(by_threshold["99.00"]["sample"]["orders"], 1)
        self.assertEqual(by_threshold["99.00"]["rules"][0]["id"], "R-old")
        self.assertEqual(by_threshold["79.00"]["rules"][0]["id"], "R-new")

    def test_integrity_flags_mismatch(self):
        orders = [{"id": "O1", "region": "片区0", "rule": "R1", "amount": "80.00"}]
        lines = [["L1", "O1", "S", 1, "50.00", "0.00"]]  # 与 80 不符
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]],
            orders, lines=lines)
        integrity = bunching.study(catalog, threshold="100")["integrity"]
        self.assertFalse(integrity["clean"])
        self.assertEqual(integrity["problems"]["line_sum_mismatch"], 1)

    def test_translation_is_a_bound_not_a_forecast(self):
        prices = [10, 20, 25, 30, 32, 33, 40, 45, 50, 60, 70, 80, 90]
        orders = []
        lines = []
        index = 0

        def add(price, quantity):
            nonlocal index
            amount = price * quantity
            orders.append({"id": f"O{index}", "region": "片区0", "rule": "R1",
                           "amount": f"{amount:.2f}"})
            lines.append([f"L{index}", f"O{index}", "S", quantity, f"{price:.2f}", "0.00"])
            index += 1

        for step in range(1400):
            add(prices[step % len(prices)], (1, 2, 3)[step % 3])
        for _ in range(200):
            add(33, 3)
        rules = [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"],
                 ["R2", "片区1", "2026-01-01", "2026-12-31", "250", "商品促销后配送券前", "8.00"]]
        catalog = self.dataset(rules, orders, lines=lines)
        economics = bunching.study(catalog, threshold="100")["thresholds"][0]["economics"]
        translation = economics["translation"]
        self.assertIsNotNone(translation)
        self.assertFalse(translation["incremental_sales_identified"])
        self.assertTrue(translation["multiplying_window_net_revenue_overstates_new_sales"])
        self.assertIn("不是新增销售预测", translation["note"])
        self.assertIn("fulfillment_cost_if_baskets_merely_modified", translation)

    def test_placebo_excludes_real_and_other_thresholds(self):
        rules = [
            ["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"],
            ["R2", "片区0", "2026-01-01", "2026-12-31", "200", "商品促销后配送券前", "8.00"],
        ]
        orders = []
        for index in range(60):
            orders.append({"id": f"O{index}", "region": "片区0", "rule": "R1", "amount": "100.00"})
        catalog = self.dataset(rules, orders)
        center = bunching._placebo_centers(10000, 2000, [10000, 20000], [20000])
        self.assertNotIn(10000, [value for value, _ in center])
        self.assertNotIn(20000, [value for value, _ in center])

    def test_frequency_is_per_order_not_per_line(self):
        orders = [{"id": "O1", "region": "片区0", "rule": "R1", "amount": "90.00"}]
        lines = [["L1", "O1", "S1", 1, "40.00", "0.00"], ["L2", "O1", "S2", 1, "50.00", "0.00"]]
        catalog = self.dataset(
            [["R1", "片区0", "2026-01-01", "2026-12-31", "100", "商品促销后配送券前", "8.00"]],
            orders, lines=lines)
        sample = bunching.study(catalog, threshold="100")["thresholds"][0]["sample"]
        self.assertEqual(sample["multi_line_excluded"], 1)
        self.assertEqual(sample["single_line_records"], 0)


if __name__ == "__main__":
    unittest.main()
