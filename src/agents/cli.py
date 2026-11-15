"""The `agents` command: run | status | report | cleanup | install | serve."""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agents import config, engine, log, prompts, server, worktrees
from agents.harness import base as harness_base
from agents.harness import get_harness
from agents.harness.claude import MCP_SERVER_NAME


def main() -> int:
    parser = argparse.ArgumentParser(prog="agents", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="start a run in this repo and open the root planner")
    run.add_argument("prompt", nargs="?", default="", help="the first message for the planner")
    run.set_defaults(handler=cmd_run)

    status = commands.add_parser("status", help="show a run's tree (the latest run by default)")
    status.add_argument("run_id", nargs="?")
    status.set_defaults(handler=cmd_status)

    report = commands.add_parser("report", help="summarise the log per tier and per kind")
    report.set_defaults(handler=cmd_report)

    cleanup = commands.add_parser("cleanup", help="remove a run's worktrees")
    cleanup.add_argument("run_id")
    cleanup.add_argument("--branches", action="store_true", help="delete the run's branches too")
    cleanup.set_defaults(handler=cmd_cleanup)

    install = commands.add_parser("install", help="register the delegate server with Claude Code")
    install.add_argument("--scope", default="user", choices=["local", "user", "project"])
    install.set_defaults(handler=cmd_install)

    serve = commands.add_parser("serve", help="run the delegate MCP server on stdio")
    serve.set_defaults(handler=cmd_serve)

    args = parser.parse_args()
    try:
        return args.handler(args)
    except (config.ConfigError, worktrees.GitError, KeyError) as error:
        print(f"agents: {error}", file=sys.stderr)
        return 1


def cmd_run(args: argparse.Namespace) -> int:
    """Start a run, then replace this process with the planner's CLI in the root worktree."""
    settings = config.load()
    caller = engine.start_run(Path.cwd(), settings)
    planner = settings.tiers[0].splitter_agent()
    budget = settings.limits.run_budget_usd
    env = engine.context_env(
        settings,
        run_id=caller.run_id,
        node_id=caller.node_id,
        depth=caller.depth,
        budget_usd=budget,
        worktree=caller.worktree,
    )
    request = harness_base.NodeRequest(
        role=harness_base.SPLITTER,
        model=planner.model,
        effort=planner.effort,
        system_prompt=prompts.load(harness_base.SPLITTER),
        prompt=args.prompt,
        worktree=caller.worktree,
        budget_usd=budget,
        env=env,
        delegate_server=engine.SERVER_COMMAND,
        allowed_commands=[],
    )
    command = get_harness(planner.harness).build_planner_command(request)

    print(f"Run      {caller.run_id}")
    print(f"Branch   {worktrees.branch_name(caller.run_id, caller.node_id)}")
    print(f"Worktree {caller.worktree}")
    print(f"Budget   ${budget:.2f}")
    os.chdir(caller.worktree)
    os.execvpe(command.argv[0], command.argv, {**os.environ, **env})


def cmd_status(args: argparse.Namespace) -> int:
    run_id = args.run_id or engine.latest_run_id()
    if run_id is None:
        print("No runs yet.")
        return 0
    print(format_status(engine.status(run_id)))
    return 0


def format_status(status: dict[str, Any]) -> str:
    """The run's tree as indented text, children under their parents."""
    left = status["run_budget_usd"] - status["run_spent_usd"]
    lines = [
        f"Run {status['run_id']} in {status['repo']}",
        f"Budget ${status['run_budget_usd']:.2f}, spent ${status['run_spent_usd']:.2f}, "
        f"left ${left:.2f}",
        "",
    ]
    for node in sorted(status["nodes"], key=lambda node: _sort_key(node["node_id"])):
        depth = node["node_id"].count(".")
        name = "  " * depth + node["node_id"]
        merge = node["merge"] if node["merge"] != "not merged" else ""
        lines.append(
            f"{name:<16} {node['tier']:<8} {node['status']:<11} {merge:<8} "
            f"${node['cost_usd']:>6.2f}  {node['kind']:<11} {node['task']}"
        )
    return "\n".join(lines)


def _sort_key(node_id: str) -> list[int]:
    """Sort root.2 before root.10, and children straight after their parent."""
    return [int(part) for part in node_id.split(".")[1:]]


def cmd_report(args: argparse.Namespace) -> int:
    print(log.report(log.read()))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    removed = engine.cleanup(args.run_id, delete_branches=args.branches)
    for item in removed:
        print(f"removed {item}")
    if not args.branches:
        print(f"Kept the branches under agents/{args.run_id}/ (pass --branches to delete them).")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Register the delegate server, so a plain `claude` session can act as the planner."""
    command = [
        "claude",
        "mcp",
        "add",
        "--scope",
        args.scope,
        MCP_SERVER_NAME,
        "--",
        *engine.SERVER_COMMAND,
    ]
    print(" ".join(command))
    return subprocess.run(command, check=False).returncode


def cmd_serve(args: argparse.Namespace) -> int:
    server.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
