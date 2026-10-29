"""``tg-desk``: run the desk page, check moomoo, and decide on proposals from a terminal."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from trading_god.broker import BrokerError


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _desk():
    from trading_god.factory import build_desk

    return build_desk()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from trading_god.desk import watch_exits
    from trading_god.web import create_app

    desk = _desk()
    host, port = desk.settings.web.host, desk.settings.web.port
    if host not in ("127.0.0.1", "localhost"):
        print("web.host must be 127.0.0.1 or localhost: the desk page is for this computer only.", file=sys.stderr)
        return 2
    print(f"Trading God desk: http://{host}:{port}  (Ctrl+C to stop)")
    stop = threading.Event()
    exits, swing = desk.settings.exits, desk.settings.swing
    if exits.auto_stop or swing.auto_target or swing.auto_time_exit:
        print("Exit plans: stops (and swing targets and last days) are sold at market during market hours.")
        threading.Thread(target=watch_exits, args=(desk, stop), name="exit-watch", daemon=True).start()
    try:
        uvicorn.run(create_app(desk), host=host, port=port, log_level="warning")
    finally:
        stop.set()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    desk = _desk()
    try:
        report = desk.broker.describe_accounts()
    except BrokerError as exc:
        _print({"status": "error", "error": str(exc)})
        return 1
    _print(report)
    if not report.get("gateway_open"):
        return 1
    try:
        portfolio = desk.portfolio()
    except BrokerError as exc:
        _print({"status": "error", "error": str(exc)})
        return 1
    account = portfolio["account"]
    print(
        f"\nOK: paper account {account['acc_id']} has ${account['equity']:,.2f} equity and "
        f"${account['cash']:,.2f} cash across {len(portfolio['positions'])} positions."
    )
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    try:
        _print(_desk().portfolio())
    except BrokerError as exc:
        _print({"status": "error", "error": str(exc)})
        return 1
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _print(_desk().history(args.status, args.limit))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    desk = _desk()
    proposal = desk.store.get(args.id)
    if proposal is None:
        print(f"No proposal #{args.id}.", file=sys.stderr)
        return 1
    from trading_god.desk import describe

    print(f"{describe(proposal)}\nPlaybook: {proposal.playbook}\n\n{proposal.thesis}\n")
    if not args.yes and input("Place this order on the moomoo PAPER account? [y/N] ").strip().lower() != "y":
        print("Nothing placed.")
        return 1
    result = desk.approve(args.id, args.note)
    _print(result)
    return 0 if result["status"] == "placed" else 1


def cmd_reject(args: argparse.Namespace) -> int:
    result = _desk().reject(args.id, args.note)
    _print(result)
    return 0 if result["status"] == "rejected" else 1


def cmd_schedule(args: argparse.Namespace) -> int:
    from trading_god.config import load_settings
    from trading_god.schedule import job_payload, load_watchlist, register

    settings = load_settings()
    watchlist = load_watchlist()
    if args.dry_run:
        _print(job_payload(settings, watchlist))
        return 0
    try:
        job = register(settings, watchlist, api_key=args.api_key)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        f"Scheduled '{job.get('title', 'pre-market scan')}' at '{settings.schedule.cron}' "
        f"({settings.schedule.timezone}) for {len(watchlist)} tickers."
    )
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    from trading_god.config import load_settings
    from trading_god.schedule import load_watchlist, render_prompt

    print(render_prompt(load_settings(), load_watchlist()))
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    from trading_god.setup_vibe import install_missing_playbooks, install_own_skills, refresh_desk_tools, vibe_home

    user_skills = vibe_home() / "skills" / "user"
    try:
        installed = install_own_skills(user_skills)
        tools_changed = refresh_desk_tools(vibe_home() / "agent.json")
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Could not refresh the skills: {exc}", file=sys.stderr)
        return 1
    print(f"Refreshed {', '.join(installed)}." + (" Updated the agent's desk tools." if tools_changed else ""))
    try:
        added = install_missing_playbooks(user_skills)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Could not add the new playbooks (the agent works without them): {exc}", file=sys.stderr)
        return 1
    if added:
        print(f"Added playbooks: {', '.join(added)}.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from trading_god.setup_vibe import configure

    try:
        summary = configure(
            desk_python=args.desk_python or sys.executable,
            openbb_url=args.openbb_url,
            model=args.model,
            claude_path=args.claude_path or None,
            investorskills=Path(args.investorskills) if args.investorskills else None,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    _print(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    from trading_god.setup_vibe import DEFAULT_MODEL, DEFAULT_OPENBB_URL

    parser = argparse.ArgumentParser(prog="tg-desk", description="Trading God approval desk (moomoo paper account)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the desk page on 127.0.0.1").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="check moomoo OpenD and the paper account").set_defaults(func=cmd_check)
    sub.add_parser("portfolio", help="print the paper account, positions and limits").set_defaults(func=cmd_portfolio)

    p = sub.add_parser("list", help="list recent proposals")
    p.add_argument("--status", default="", help="e.g. pending or placed,rejected")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("approve", help="approve one proposal and place it on the paper account")
    p.add_argument("id", type=int)
    p.add_argument("--note", default="")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject one proposal")
    p.add_argument("id", type=int)
    p.add_argument("--note", default="", help="tell the agent why; it reads this next time")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("schedule", help="register the pre-market scan with Vibe-Trading")
    p.add_argument("--dry-run", action="store_true", help="print the job instead of sending it")
    p.add_argument("--api-key", default="", help="Vibe-Trading API_AUTH_KEY, if you set one")
    p.set_defaults(func=cmd_schedule)

    sub.add_parser(
        "prompt", help="print the pre-market scan prompt, to run a scan by hand in Vibe-Trading"
    ).set_defaults(func=cmd_prompt)

    sub.add_parser(
        "skills", help="refresh the agent's Trading God skill after a git pull (run by start.ps1)"
    ).set_defaults(func=cmd_skills)

    p = sub.add_parser("setup", help="configure Vibe-Trading for Trading God (run by install.ps1)")
    p.add_argument("--desk-python", default="", help="python.exe of this environment (default: the current one)")
    p.add_argument("--openbb-url", default=DEFAULT_OPENBB_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--claude-path", default="")
    p.add_argument("--investorskills", default="", help="local investorskills checkout instead of downloading it")
    p.set_defaults(func=cmd_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
