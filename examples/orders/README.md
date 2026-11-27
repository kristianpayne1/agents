# The orders example

A small repo to try the tree on. It has a little order-parsing code, its tests, and
`TASK.md`: add `load_orders()` and `summarise()`.

The task leaves some choices open on purpose, so that a run has decisions to make and
questions to ask:

- what to do with a line that can't be read (requirement 3 only says it mustn't go unnoticed)
- how to break a tie for best seller
- what `summarise([])` gives

## Use it

```
agents example ~/tmp/orders
cd ~/tmp/orders
agents run "Do the task in TASK.md"
agents decisions
```

`agents example` copies `repo/` to a new folder and makes it a git repo with one commit.

## Judge a result

`accept/` holds acceptance tests that check only what the task settles. They are never
copied into the repo, so no node sees them. `agents bench` copies them over each finished
result:

```
agents bench --task TASK.md --test-cmd "python3 -m unittest" \
  --accept-dir <this repo>/examples/orders/accept --accept-cmd "python3 -m unittest test_accept"
```

## Tests

`tests/test_example.py` checks that the example starts green and that the acceptance tests
fail until the task is done. It also plays a whole run on it with the fake harness: a leaf
asks a question, is answered, and finishes. `tests/test_live.py` has an opt-in run with real
models (`uv run pytest -m live -s -k orders`).
