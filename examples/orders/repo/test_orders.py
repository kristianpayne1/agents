import unittest
from decimal import Decimal

from orders import Order, parse_line


class ParseLineTest(unittest.TestCase):
    def test_reads_every_field(self) -> None:
        self.assertEqual(parse_line("A1,widget,3,2.50"), Order("A1", "widget", 3, Decimal("2.50")))

    def test_ignores_spaces_around_fields(self) -> None:
        self.assertEqual(parse_line(" A1 , widget , 3 , 2.50 ").sku, "widget")

    def test_total_is_price_times_quantity(self) -> None:
        self.assertEqual(parse_line("A1,widget,3,2.50").total, Decimal("7.50"))


if __name__ == "__main__":
    unittest.main()
