# Load and summarise a day's orders

Add the following to `orders.py`, with unit tests in `test_orders.py`. The tests run with
`python3 -m unittest`.

1. `load_orders(path)` reads a file of orders and returns them as a list of `Order`, in the
   order they appear. `sample_orders.csv` shows what a real file looks like.
2. `summarise(orders)` returns a `Summary` with three fields: `count` (the number of orders),
   `total` (their combined value, a `Decimal`) and `best_seller` (the SKU with the largest
   total quantity).
3. A line that can't be read must not go unnoticed.
4. Don't change `parse_line` or the fields of `Order`: other code depends on them.
