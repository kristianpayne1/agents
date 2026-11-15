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
agents status              # the latest run's tree: status, cost and merge state per node
agents status <run_id>
agents report              # cost and pass rate per tier and per kind, from the log
agents cleanup <run_id>    # remove the run's worktrees; add --branches to delete its branches too
```

`agents install` registers the delegate server with Claude Code, so that a plain `claude` session can act as the planner without `agents run`. The server then starts the run the first time a tool is called.

## Configure

`roles.toml` lists the tiers, root first, and the limits:

| Limit | Meaning |
| --- | --- |
| `max_depth` | the depth at which every node is a leaf (2 means planner, heavy, leaf) |
| `run_budget_usd` | what one run may spend |
| `leaf_max_files` | the most files a leaf task may list |
| `leaf_max_task_words` | the most words in a leaf's task plus contract |
| `leaf_max_attempts` | how many times a leaf is tried before its task fails |
| `node_timeout_s`, `test_timeout_s` | time limits for one agent process and one test run |

The config is read from `$AGENTS_CONFIG`, then `~/.config/agents/roles.toml`, then the `roles.toml` in this repo. State goes under `$AGENTS_STATE_DIR`, or `~/.local/state/agents` by default.

## How it works

**Tiers, not models.** A child's tier comes from its depth in the tree, and the tier's model comes from `roles.toml`. Splitters never name a model, and neither does any code or prompt here.

**Task-size checks.** A leaf task must list its files (no more than `leaf_max_files`), give a test command, and fit in `leaf_max_task_words`. If it doesn't, `delegate` returns `refused` and nothing runs. The splitter has to split further.

**Tests decide.** After a leaf exits, the server runs its test command. If it fails, the leaf is run again with the test output, up to `leaf_max_attempts`. Then the task is `failed`. If a node leaves work uncommitted, the server commits it.

**Merging is a separate step.** `delegate` returns a child's result; `integrate` merges it. In between, the parent reviews. Merges use `--no-ff`, one child at a time. On a conflict, `integrate` aborts that merge and hands the conflict back.

**Budgets.** A `delegate` call reserves part of the caller's remaining budget for the child's subtree, or returns `over_budget`. When the child finishes, what it didn't spend goes back. Costs come from the CLI's own report (`total_cost_usd`), which is the list price of the tokens used.

**Run state** is one small SQLite file per run, shared by every delegate server in the run.

**The log.** Each finished node writes one line to `~/.local/state/agents/log.jsonl`: run, node, parent, depth, tier, model, kind, file count, task word count, attempts, status, cost, tokens, permission denials, duration and test exit code. `integrate` calls are logged too. This log is the main output of phase 1.

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
