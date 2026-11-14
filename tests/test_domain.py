import unittest
from basketbunch.catalog import Catalog
from basketbunch.domain import amount_histogram


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")

    def test_histogram_excludes_cancelled_orders(self):
        self.assertEqual(sum(record["orders"] for record in amount_histogram(self.catalog)), sum(not record["cancelled"] for record in self.catalog.rows("orders")))
        self.assertTrue(any(float(record["refund"]) > 0 for record in self.catalog.rows("settlements")))


    def test_decimal_sort_not_lexical_sort(self):
        records = [{"qualifying_amount": amount, "cancelled": False} for amount in ("100.00", "9.00", "20.00")]
        self.assertEqual([record["amount"] for record in amount_histogram(FakeCatalog({"orders": records}))], ["9.00", "20.00", "100.00"])

    def test_refund_does_not_mutate_qualifying_amount(self):
        records = [{"qualifying_amount": "99.00", "cancelled": False}]
        catalog = FakeCatalog({"orders": records, "settlements": [{"refund": "50.00"}]})
        self.assertEqual(amount_histogram(catalog), [{"amount": "99.00", "orders": 1}])

    def test_empty_completed_orders(self):
        self.assertEqual(amount_histogram(FakeCatalog({"orders": []})), [])

    def test_duplicate_amounts_are_frequencies(self):
        records = [{"qualifying_amount": "99.00", "cancelled": False} for _ in range(3)]
        self.assertEqual(amount_histogram(FakeCatalog({"orders": records}))[0]["orders"], 3)


class FakeCatalog:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, name):
        return self.tables[name]
