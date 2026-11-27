"""Acceptance tests for the orders example. The tree never sees these.

They check only what TASK.md settles. The choices it leaves open (what a bad line does, how
a tie for best seller is broken, what an empty list gives) are left to the run, and show up
in `agents decisions`.
"""

import unittest
from decimal import Decimal
from pathlib import Path

from orders import Order, load_orders, parse_line, summarise

SAMPLE = Path(__file__).parent / "sample_orders.csv"


class AcceptanceTest(unittest.TestCase):
    def test_sample_file_loads_in_order(self) -> None:
        orders = load_orders(SAMPLE)

        self.assertEqual([order.order_id for order in orders], ["A1", "A2", "A3", "A4", "A5"])
        self.assertTrue(all(isinstance(order, Order) for order in orders))

    def test_sample_file_summary(self) -> None:
        summary = summarise(load_orders(SAMPLE))

        self.assertEqual(summary.count, 5)
        self.assertEqual(summary.total, Decimal("37.50"))
        self.assertEqual(summary.best_seller, "widget")

    def test_parse_line_is_unchanged(self) -> None:
        self.assertEqual(parse_line("A1,widget,3,2.50"), Order("A1", "widget", 3, Decimal("2.50")))


if __name__ == "__main__":
    unittest.main()
