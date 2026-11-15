# 包邮门槛附近的凑单聚集资料约定

规则由 rule_id 锁定，不按查询当天门槛重算历史。促销折扣已经进入 qualifying_amount，配送券与事后退款另列。商品价格保留自然尾数，数据没有标记哪些订单是因包邮而新增。gross qualifying amount 与净收入不同。计量的最小金额刻度为人民币一分，聚合到整元时需声明舍入，不可把同一订单扩成多个独立频次。

## 数据表

### `shipping_rules`

生效期左闭右开，包邮为 qualifying_amount 大于等于 threshold；配送券不改变凑单运行变量。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `region` | `text` |
| `valid_from` | `date` |
| `valid_to` | `date` |
| `threshold` | `money` |
| `amount_basis` | `text` |
| `shipping_fee` | `money` |

### `orders`

顾客确认时的计费资格金额，单位元，精确到分；取消订单与成交订单仍可分别研究。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `created_at` | `time` |
| `region` | `text` |
| `rule_id` | `text` |
| `qualifying_amount` | `money` |
| `delivery_coupon` | `money` |
| `cancelled` | `bool` |

### `order_lines`

单行商品折后总额为 quantity 乘 unit_price 减 discount，discount 为整行折扣。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `order_id` | `text` |
| `sku` | `text` |
| `quantity` | `int` |
| `unit_price` | `money` |
| `discount` | `money` |

### `settlements`

事后退款及成本用于经营解释，不改写顾客当时看到的计费资格金额；每单一份最新核准结算。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `order_id` | `text` |
| `day` | `date` |
| `refund` | `money` |
| `fulfillment_cost` | `money` |
| `product_cost` | `money` |

## 文件和数值语义

空单元格只在带问号的类型中表示缺失；零和空值不同。金额由十进制定点字符串保存，浮点数仅用于探索性统计。所有数据均为本地生成的虚构经营记录，不对应真实公司、客户或住址。数值剖面只反映观察数据，不能代替总体推断。

研究备注的参数为业务自由对象，不代表已执行了任何统计模型。原始表与已有视图可单独查询。

## 门槛聚集研究的计量口径

研究实现在 `basketbunch/bunching.py`，接口见 `docs/api.md`。

* **运行变量**：`qualifying_amount`。配送券与事后退款只影响净收入，不参与门槛归类；
  若把它们从金额里扣除，报告多少单会跨线，但不重分类。
* **门槛锁定**：按订单 `rule_id` 找当时的 `threshold`，同一门槛可跨地区、跨日期由多条
  规则组成。只有实际生效过的门槛才有断点；对未生效门槛请求估计时返回 `out_of_support`。
* **金额刻度**：以“分”精确分箱，并报告可观测金额的最小步长、是否全为整元、商品单价×
  数量的可达金额与碰撞。只在刻度上求平均密度或连续平滑没有直接意义。
* **反事实**：主口径是商品价格×数量的独立性构成模型。`composition` 用全样本边际，凑单
  会抬高边际、压低超额，是保守下限；`composition_excluded` 只用窗口外格估计边际，给出
  上界方向，若某价格或数量在窗口外没有观测则不可识别并明确报告。`poisson1`/`poisson2`
  是连续平滑对照，不单独决定结论。
* **诊断**：规格曲线（窗口×排除区间×拟合选择）、规则外虚假门槛（金额刻度与中点，排除
  真实门槛与其它已生效门槛）、留出区间拟合、跨门槛期留出反事实、跨期同一刻度对照。
* **识别结论**：只有 `supported` 与 `insufficient`。保守下限不显著为正、规格曲线符号
  不稳、真实门槛超额落在虚假门槛分布中段、同一刻度在另一门槛期同样密集、跨期留出反
  事实下超额不显著、门槛任一侧窗口内可观测金额点不足，或没有对照期时，一律报告
  `insufficient`。
* **经营解释**：报告窗口订单的毛额、运费、配送券、退款、商品成本、履约成本与每单净
  收入；另给“最多可能有多少单”的会计上界。上界不是凑单量，`incremental_orders_identified`
  与 `incremental_sales_identified` 在识别不足时均为假。聚集量不等于新增销售，也不从
  未观测过的门槛推断收益。

