from collections import Counter
from decimal import Decimal


def amount_histogram(catalog):
    counts = Counter(record["qualifying_amount"] for record in catalog.rows("orders") if not record["cancelled"])
    return [{"amount": amount, "orders": count} for amount, count in sorted(counts.items(), key=lambda item: Decimal(item[0]))]


def overview(catalog):
    return {"规则数": len(catalog.rows("shipping_rules")), "成交金额频数": amount_histogram(catalog), "取消单数": sum(record["cancelled"] for record in catalog.rows("orders"))}
