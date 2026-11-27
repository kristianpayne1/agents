# agents

A delegation tree for coding agents, driven from the terminal.

- A **planner** at the root plans the work and reviews the result.
- **Heavy splitters** break a feature into small, testable tasks, then review and merge what comes back.
- **Leaves** do one small task each and never delegate.

Every splitter, the planner included, gets one MCP server: the **delegate server**. It starts children in their own git worktrees, enforces task sizes and budgets, merges branches back up and logs every node. Which model plays which tier is set in one file, `roles.toml`.

This is phase 1, a proof of concept: one machine, one harness (the `claude` CLI), no local models. Its job is to show that the tree works and to collect the log that later decisions will be based on.

```
your terminal
 └─ planner (claude)                     has the delegate server
      └─ delegate ──► heavy splitter     headless, own worktree, has the delegate server
                        └─ delegate ──► leaf     headless, own worktree, no delegate tool
```

## Install

You need git, [uv](https://docs.astral.sh/uv/) and the `claude` CLI, signed in.

```
git clone <this repo> ~/Projects/agents
uv tool install --editable ~/Projects/agents
```

That puts an `agents` command on your PATH that runs straight from the checkout.

## Use

From the repo you want changed:

```
agents run "Add a slugify() helper with tests"
```

This starts a run:

1. It creates a branch `agents/<run_id>/root` from your current `HEAD`, checked out in a worktree under `~/.local/state/agents/worktrees/`. Your own checkout is never touched.
2. It opens the planner there, with the delegate server attached.
3. The planner delegates to heavy splitters, which delegate to leaves. Each node works on its own branch (`agents/<run_id>/root.1`, `root.1.2`, and so on) and results are merged back up, with tests run at each level.
4. When the planner is done, review the root branch in lazygit and merge it yourself. Nothing is ever pushed.

While it runs, or afterwards:

```
agents status              # the latest run's tree: status, cost, merge state and decisions per node
agents status <run_id>
agents decisions           # every decision, question and answer in the latest run, and how to undo each
agents report              # cost and pass rate per tier and per kind, from the log
agents cleanup <run_id>    # remove the run's worktrees; add --branches to delete its branches too
agents bench ...           # compare the tree with single-model sessions (see below)
agents example <folder>    # make a small repo to try the tree on (see examples/orders/)
```

`agents install` registers the delegate server with Claude Code, so that a plain `claude` session can act as the planner without `agents run`. The server then starts the run the first time a tool is called.

## Try it on the example

`examples/orders/` is a small repo with a task that leaves a few choices open on purpose, so a run has decisions to make and questions to ask:

```
agents example ~/tmp/orders
cd ~/tmp/orders
agents run "Do the task in TASK.md"
agents decisions
```

It also has hidden acceptance tests for `agents bench`, and `tests/test_example.py` plays a whole run on it with the fake harness. See `examples/orders/README.md`.

## Compare with a single session

`agents bench` runs the same task through the tree and through plain single-model sessions, so you can see whether the tree is worth it for that kind of task.

```
agents bench --task task.md --test-cmd "uv run pytest" \
  --accept-dir ~/bench/hidden-tests --accept-cmd "uv run pytest tests/acceptance"
```

- **Arms.** `tree` is the full tree with the planner run headless. `solo:<tier>` is one headless session using that tier's model, working alone. By default it runs the tree and one solo arm per splitter tier; choose with `--arms tree,solo:heavy`.
- **Same start, same rules.** Every arm starts from your current `HEAD`, gets the task file word for word, and has the same permissions as a node in the tree.
- **Judged by tests the arms never see.** After an arm finishes, the files in `--accept-dir` are copied over its result and `--accept-cmd` decides pass or fail. Write those tests before you start.
- **Repeated.** Each arm runs `--runs` times (3 by default), one run after another, taking turns between the arms.
- **Reviewable blind.** Each result is left as a branch, `bench/<bench_id>/<number>`, with the numbers shuffled. Look at the diffs first; which arm wrote which is in the results file it prints the path of.

It prints the average cost, time and diff size per arm, and how many runs were accepted. It makes real model calls, so it says the most it could cost and asks before starting. To remove the branches afterwards: `git branch --list 'bench/*' | xargs git branch -D`.

Pick tasks where the answer isn't obvious: one with many independent pieces (backfilling tests, a rename across many files), where the tree should win, and one deep change that needs the whole codebase in mind, where it probably won't.

## Configure

`roles.toml` lists the tiers, root first, and the limits:

| Limit | Meaning |
| --- | --- |
| `max_depth` | the depth at which every node is a leaf (2 means planner, heavy, leaf) |
| `run_budget_usd` | what one run may spend |
| `leaf_max_files` | the most files a leaf task may list |
| `leaf_max_task_words` | the most words in a leaf's task plus contract |
| `leaf_max_attempts` | how many times a leaf is tried before its task fails |
| `max_question_rounds` | how many of a splitter's `delegate`, `delegate_many` and `answer` calls may bring back questions before it has to pass what's open up to its own parent (the planner is exempt) |
| `node_timeout_s`, `test_timeout_s` | time limits for one agent process and one test run |

The config is read from `$AGENTS_CONFIG`, then `~/.config/agents/roles.toml`, then the `roles.toml` in this repo. State goes under `$AGENTS_STATE_DIR`, or `~/.local/state/agents` by default.

## How it works

**Tiers, not models.** A child's tier comes from its depth in the tree, and the tier's model comes from `roles.toml`. Splitters never name a model, and neither does any code or prompt here.

**Task-size checks.** A leaf task must list its files (no more than `leaf_max_files`), give a test command, and fit in `leaf_max_task_words`. If it doesn't, `delegate` returns `refused` and nothing runs. The splitter has to split further. Each splitter's prompt states the limits, so it can write within them.

**Running children together.** `delegate_many` starts a list of children at once on the server and returns when the last is done. Separate `delegate` calls only overlap if the agent CLI sends tool calls together.

**Decisions go up the tree.** When a node meets a choice its task and contract don't settle, it sorts it by risk:

- *Low-risk*: undoing it means reverting that node's work and nothing else, and no other task is written against it. A splitter makes the choice, carries on, and lists it under `## Decisions made`. `delegate` returns the list as `decisions` for the parent to review before it merges, and `integrate` writes it into the merge commit, so a revert of that commit undoes it.
- *Not low-risk*: it changes a contract, a data format or anything stored, adds a dependency, changes behaviour that existing code or users rely on, touches security, or settles contradicting instructions. The node stops and ends its message with a `## Decision needed` section. `delegate` returns `needs_decision` with the question, and the leaf isn't retried. The parent decides it if it's within the parent's own task and replies with `answer`: the child carries on in its own session and worktree, so nothing is redone. Otherwise the parent asks its own parent in the same way. At the top, the planner asks you.

Nothing goes round in circles. A splitter only answers a question it is sure of, never answers the same question twice, and never lets two children settle a choice between themselves: anything it is unsure of goes up. Once `max_question_rounds` of a splitter's calls have brought back questions (children asking in the same call count once), the server refuses its further `delegate` and `answer` calls, so it has to merge what it can and pass what is open up. The planner is exempt, because its questions go to you.

Leaves, the cheapest tier, make no decisions at all: they ask about every open choice, low-risk ones included. A leaf that lists decisions it made anyway comes back `needs_decision`, and its parent merges the work only if it agrees.

**The planner settles the design with you and has the result reviewed.** Before delegating, it puts the task's open questions that aren't low-risk to you, with the option it would pick for each, lists the low-risk ones it decided so you can overrule them, and writes the answers into the contract. It doesn't take its delegates' decisions on trust: it checks each one against the task, the contract, the diff and your answers, then keeps it, overrules it, or brings it to you when it is unsure. Its final summary lists every decision made in the tree and what happened to each. When everything is merged, it delegates one `review` child to check the branch against the task's numbered requirements and to find choices in the diff that no node recorded, then fixes what that finds. Its summary marks the unrecorded decisions, so you know which nodes decided things without saying so.

**Tests decide.** After a leaf exits, the server runs its test command. If it fails, the leaf is run again with the test output, up to `leaf_max_attempts`. Then the task is `failed`. If a node leaves work uncommitted, the server commits it.

**Merging is a separate step.** `delegate` returns a child's result; `integrate` merges it. In between, the parent reviews. Merges use `--no-ff`, one child at a time. On a conflict, `integrate` aborts that merge and hands the conflict back.

**Budgets.** A `delegate` call reserves part of the caller's remaining budget for the child's subtree, or returns `over_budget`. When the child finishes, what it didn't spend goes back. Costs come from the CLI's own report (`total_cost_usd`), which is the list price of the tokens used.

**Run state** is one small SQLite file per run, shared by every delegate server in the run.

**The log.** Each finished node writes one line to `~/.local/state/agents/log.jsonl`: run, node, parent, depth, tier, model, kind, file count, task word count, attempts, status, cost, tokens, permission denials, duration, test exit code and how many decisions it made itself. `integrate` calls are logged too. This log is the main output of phase 1.

## What headless nodes may do

Heavy splitters and leaves run with nobody to answer a permission prompt, so their permissions are fixed up front:

- They may edit files in their worktree.
- They may run their test command exactly as written, and `git status`, `diff`, `log`, `show`, `add` and `commit`.
- The CLI also allows read-only commands (`ls`, `cat`) and simple file commands (`touch`, `mkdir`) in the worktree by itself.
- Everything else is denied, including `git push`, `merge` and `reset`, package installs and network tools.
- Leaves get no MCP servers and no sub-agent tool, so they can't delegate.

Three things to know:

- A node can only run its own test command when a permission rule can express it. A plain command (`pytest tests/test_orders.py`) works. One with quotes, pipes or several steps is often denied, so the node can't check its own work. The server still runs the command afterwards and that run decides the result, but the node only learns the outcome through a retry. The log counts permission denials per node, so you can see how often this happens.

- A test command is a shell command that a splitter wrote, and the server runs it as you. Run this on repos and tasks you'd trust an agent with.
- "Only edit the listed files" is an instruction to the leaf, not a lock. If a leaf edits another file, its result carries a warning for the splitter to act on.

## Known limits

- The planner's own session isn't counted against the budget. It is interactive, so its cost isn't reported to the server. Everything it delegates is counted.
- A splitter's own spending is only known when it finishes. Its CLI is capped at its budget, but its own cost plus its children's can end up over it. The overrun is recorded and charged to the parent.
- If a splitter process is killed, budget its children had reserved stays reserved for the rest of the run.
- A new worktree has only what is committed. Dependencies that live in untracked folders (`node_modules`, a virtualenv) aren't there, and nodes can't run an install command. Use a test command that works in a fresh checkout, such as `uv run pytest`, which sets up its own environment.
- Untracked files that a test run leaves behind are committed with the node's work unless the repo ignores them. Keep build output in `.gitignore`.

## Develop

Tool versions are pinned in `mise.toml` and `pyproject.toml`, and locked with checksums in `mise.lock` and `uv.lock`.

```
uv sync
uv run pytest                    # no test here calls a model
uv run ruff check . && uv run ruff format --check .
uv run basedpyright
uv run python scripts/check_model_names.py
uv run pytest -m live -s         # one real end-to-end run on a toy repo; costs money
```

Tests use a fake harness: an adapter that runs a small Python script in place of a model, making scripted edits and commits. See `AGENTS.md` for the layout and the rules to keep.
