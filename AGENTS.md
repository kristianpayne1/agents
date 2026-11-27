# agents

A delegation tree for coding agents. A planner at the root plans the work and reviews the result, heavy splitters break a feature into small testable tasks, and leaves each do one small task. Everything goes through one MCP server, the delegate server, which gives every splitter four tools: `delegate`, `delegate_many`, `integrate` and `status`.

This file has two parts: how to use the tree, and how to work on this repo.

## Using the tree

Start a run from the repo you want changed:

```
agents run "Add a slugify() helper with tests"
```

That creates a worktree and a branch for the planner (`agents/<run_id>/root`), starts the planner there with the delegate server attached, and leaves your own checkout alone. When the planner is done, review the root branch in lazygit and merge it yourself. Nothing is ever pushed.

### The tools

**`delegate`** starts a child in its own worktree and waits for its result.

| Argument | Meaning |
| --- | --- |
| `task` | What to do, in plain words |
| `contract` | Interfaces, signatures and test names the child must keep to. Passed down unchanged. |
| `files` | Files the child may edit. Required for leaves. |
| `test_cmd` | The command that proves the task is done. Required for leaves. |
| `kind` | A tag for the logs: `tests`, `rename`, `boilerplate`, `component`, `migration`, `feature`, `review` or `other` |
| `budget_usd` | The slice of your remaining budget this child's subtree may use |

The server picks the child's tier from its depth: the planner's children are heavy splitters, and theirs are leaves. You never name a model. The child's branch starts from the tip of your branch at that moment, so commit before you delegate.

The result has `node_id`, `status`, `branch`, `summary`, `question`, `decisions`, `diff_stat`, `test_output`, `cost_usd` and `attempts`. The status is one of:

- `passed`: the test command passed.
- `failed`: it still failed after the leaf's retries.
- `needs_decision`: the child stopped to ask you something, and `question` says what. A child asks by ending its final message with a `## Decision needed` section. It isn't retried. Decide, write the answer into the contract and delegate again, or ask your own parent. Its work can still be merged.

`decisions` lists the low-risk decisions a splitter child made itself, from a `## Decisions made` section in its final message. Review them before you merge: if you disagree, don't merge, and delegate again with your choice in the contract. Leaves may not make decisions: a leaf that lists any comes back `needs_decision`, and its parent merges its work only if it agrees with them.
- `refused`: a leaf task broke a size rule, and nothing ran. Leaf tasks need `files` (at most `leaf_max_files`), a `test_cmd`, and a task plus contract within `leaf_max_task_words`.
- `over_budget`: you asked for more than you have left.

**`delegate_many`** takes a list of children, each with the same arguments as `delegate`, starts them all at once and returns their results in order when the last is done. Use it for independent pieces: separate `delegate` calls only overlap if the agent CLI sends tool calls together, which it may not.

**`integrate`** merges the children you name into your branch, one at a time with a merge commit, then runs a test command in your worktree. Each merge commit's message lists the decisions that child made, so reverting the commit undoes them. It's a separate tool on purpose: review a child's result before you merge it. If a merge conflicts, it stops, leaves that child unmerged and your worktree as it was, and returns the conflicting files to you. Delegate that task again (the new child starts from your updated branch) or make the change yourself. With an empty list of nodes it only runs the test command.

**`status`** shows the run's tree so far: each node's status, cost and merge state, the decisions it recorded and any question it asked, your branch and worktree, and the budget you have left.

### Splitting rules

- Split by the smallest unit that can be tested on its own: one function, one test file, one component, one migration.
- Fix the contract (types, signatures, test names) before handing out fill-in tasks. Commit it, and give the same contract text to every child that depends on it.
- Aim for tasks a person would finish in well under an hour.
- A leaf's test command has to be able to pass with only the files that leaf may edit, plus what is already committed on your branch. If it needs a file that another leaf is writing, the leaf tends to write that file too, outside its list. Order such tasks instead: merge the first, then delegate the second.
- A task that writes tests for code that isn't written yet can't be judged by running those tests: they fail until the code exists. Give it a test command that only checks the tests load (`pytest --collect-only tests/test_orders.py`), or delegate the implementation first.
- Keep a test command plain: one program and its arguments, like `pytest tests/test_orders.py`. A leaf may only run its test command exactly as written, and a command with quotes, pipes or several steps is often denied to it.
- Give siblings different files wherever you can, and run independent children in parallel with `delegate_many`.
- Once the contract is fixed, give leaves implementation as well as tests: they are the cheapest tier.
- Write leaf tasks within the limits, which each splitter's prompt states; a refusal costs a turn.
- Tag every task's `kind`.
- A splitter that does a piece of work itself gives a one-line reason in its summary.
- Decisions go up the tree, as a record or as a question. A low-risk decision (undoing it means reverting one child's work and nothing else, and no other task is written against it) is made by the splitter that meets it and listed under `## Decisions made`, so the run doesn't stall. Leaves never decide: they ask about every open choice, low-risk ones included, so a splitter settles edge cases, error messages, defaults and names in the contract before delegating. A decision that isn't low-risk (it changes a contract, a data format or anything stored, adds a dependency, changes behaviour that existing code or users rely on, touches security, or settles contradicting instructions) is a question for the parent under `## Decision needed`. The parent decides it if it's within the parent's own task, and otherwise asks its own parent. Each splitter passes up the decisions made below it that it kept. Nothing goes round in circles: a splitter only answers a question it is sure of, never answers the same question twice, and never lets children settle a choice between themselves. Anything it is unsure of goes up. After `max_questions` questions from its children, the server refuses its further `delegate` calls, so it has to pass what is open up. The planner is exempt, because its questions go to the user.
- The planner puts the task's open design questions that aren't low-risk to the user before delegating, with the option it would pick for each, and writes the answers into the contract. It lists the low-risk ones it decided in the same message, so the user can overrule them. Questions that come up from below and that nothing settles go to the user too. The planner scrutinises every decision its delegates made, against the task, the contract, the diff and the user's answers, and `status` lists them all for it. It keeps a decision, overrules it, or, when it is unsure, brings it to the user. Its final summary lists every decision made in the tree and what happened to each. When the planner runs with nobody watching (`agents bench`), it decides them itself and lists them in its summary. When everything is merged, it delegates one `review` child to check the branch against the task's numbered requirements, fixes what it finds, and lists the choices the work made in its summary.

Good leaf tasks:

- "Write unit tests for `parse_order()` covering the cases listed."
- "Implement `parse_order()` so `tests/test_orders.py` passes."
- "Rename `UserId` to `AccountId` across `src/billing/`."

The same rules, written for the splitter itself, are in `src/agents/prompts/splitter.md`. The server sends that file to every splitter, so keep the two in step.

## Working on this repo

```
uv sync                                   # install
uv run pytest                             # tests; none of them call a model
uv run ruff check . && uv run ruff format --check .
uv run basedpyright
uv run python scripts/check_model_names.py
```

Keep these true:

- **No model names outside `roles.toml`.** Code, prompts, tests and docs refer to tiers (planner, heavy, leaf). CI checks this with `scripts/check_model_names.py`.
- **Python, not shell.** Scripts and helpers are readable Python. No `.sh` files.
- **Plain over clever.** The owner reads all of it.
- **Everything pinned.** Tools in `mise.toml` and Python packages in `pyproject.toml` are at exact versions, locked with checksums in `mise.lock` and `uv.lock`. After changing a version, run `uv run python scripts/lock.py`.
- **The repo is public.** No secrets, hostnames or personal paths. Machine-specific values come from environment variables or a config file outside the repo.
- **Tests use the fake harness.** `src/agents/harness/fake_agent.py` stands in for a model and makes scripted edits, so tests cost nothing. The one test with real models is opt-in: `uv run pytest -m live -s`.
- **Never touch the user's checkout, never push.** All work happens in worktrees under the state directory.

Where things are:

| Path | What it does |
| --- | --- |
| `roles.toml` | tiers, the leaf pool and the limits |
| `src/agents/config.py` | loads and validates `roles.toml` |
| `src/agents/server.py` | the MCP server: describes the four tools |
| `src/agents/engine.py` | what the tools do: run a child, retry, merge, report |
| `src/agents/tree.py` | run state in SQLite: nodes and budgets |
| `src/agents/worktrees.py` | every git command: worktrees, branches, merges |
| `src/agents/harness/` | adapters for agent CLIs: `base.py` is the interface, `claude.py` the real one, `fake.py` the test one |
| `src/agents/router.py` | picks a member of the leaf pool |
| `src/agents/checks.py` | the task-size rules for leaves |
| `src/agents/log.py` | the JSONL log and `agents report` |
| `src/agents/prompts/` | the splitter and leaf system prompts |
| `src/agents/bench.py` | `agents bench`: the tree against single-model sessions on one task |
| `src/agents/cli.py` | `agents run`, `status`, `report`, `cleanup`, `bench`, `install`, `serve` |
