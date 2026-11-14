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
