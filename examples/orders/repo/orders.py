"""Orders for a small shop: read them and add them up."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Order:
    order_id: str
    sku: str
    quantity: int
    unit_price: Decimal

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


def parse_line(line: str) -> Order:
    """One order from a line like `A1,widget,3,2.50`."""
    order_id, sku, quantity, unit_price = (field.strip() for field in line.split(","))
    return Order(order_id, sku, int(quantity), Decimal(unit_price))
