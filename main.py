from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _load_dotenv_near_script() -> dict[str, str]:
    """Minimal .env loader from the same directory as main.py."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return {}
    env: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (len(v) >= 2) and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        if k:
            env[k] = v
    return env


async def cmd_get_updates() -> int:
    """Print Telegram chat_id from bot updates.

    This command intentionally does NOT require IMAP settings.
    It reads TELEGRAM_BOT_TOKEN from .env next to main.py.
    """
    from aiogram import Bot

    env = _load_dotenv_near_script()
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing in .env (рядом с main.py).", file=sys.stderr)
        return 2

    bot = Bot(token=token)
    try:
        updates = await bot.get_updates(limit=50)
        if not updates:
            print("No updates yet.")
            print("1) Open your bot in Telegram and send any message (e.g. 'hi').")
            print("2) Run: python main.py get-updates")
            print("If you use webhook anywhere, disable it or use a clean bot token.")
            return 0

        printed = 0
        for u in updates:
            if u.message and u.message.chat:
                print(
                    f"chat_id={u.message.chat.id} from_user={u.message.from_user.id if u.message.from_user else None}"
                )
                printed += 1

        if printed == 0:
            print("Updates exist, but no message.chat found. Try sending a plain text message to the bot.")
        return 0

    except Exception as e:
        print(f"ERROR: getUpdates failed: {e}", file=sys.stderr)
        print("Common reasons:", file=sys.stderr)
        print("- you didn't message the bot yet", file=sys.stderr)
        print("- webhook is set (getUpdates cannot be used with webhook)", file=sys.stderr)
        print("- invalid TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 1
    finally:
        await bot.session.close()


async def cmd_imap_debug(sample: int) -> int:
    """Debug IMAP state: mailbox list, ALL/UNSEEN counts and sample headers."""
    from soc_core.config import load_settings
    from soc_core.imap_client import ImapClient

    s = load_settings()
    imap = ImapClient(
        host=s.imap_host,
        port=s.imap_port,
        username=s.imap_username,
        password=s.imap_password,
        mailbox=s.imap_mailbox,
    )
    info = await imap.debug_mailbox(from_email=s.imap_from_filter, sample=sample)

    print("=== IMAP DEBUG ===")
    print(f"host={info.get('host')} port={info.get('port')} mailbox={info.get('mailbox')}")
    print(f"select_ok={info.get('selected_ok')} selected_count={info.get('selected_count')}")
    counts = info.get("counts") or {}
    print(
        f"counts: ALL={counts.get('all')} UNSEEN={counts.get('unseen')} FROM_ALL={counts.get('from_all')} UNSEEN_FROM={counts.get('unseen_from')}"
    )

    print("\n--- Mailboxes (raw LIST output) ---")
    for line in info.get("mailboxes") or []:
        print(line)

    print("\n--- Latest sample headers ---")
    for item in info.get("samples") or []:
        print(f"\n[UID {item.get('uid')}] {item.get('flags_line')}")
        print(item.get("header") or "")

    return 0


def cmd_reset_db(yes: bool) -> int:
    """
    Deletes SQLite DB file (SQLITE_PATH) to reset emails/events/dedup/assets.
    """
    from soc_core.config import load_settings

    s = load_settings()
    db_path = Path(s.sqlite_path)

    if not yes:
        print("This will DELETE the SQLite database file and reset SOCANA state:")
        print(f"  {db_path}")
        print("Re-run with: python main.py reset-db --yes")
        return 2

    if db_path.exists():
        db_path.unlink()
        print(f"OK: deleted {db_path}")
    else:
        print(f"OK: database file not found (already clean): {db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="socana")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="Run SOCANA (IMAP polling + Telegram bot)")
    sub.add_parser("run-once", help="Process Kaspersky emails once (UNSEEN by default, can use --mode latest for tests)")
    # args for run-once
    ro = sub.choices["run-once"]
    ro.add_argument("--mode", choices=["unseen", "latest"], default="unseen", help="unseen=only UNSEEN mails; latest=last N mails regardless of Seen")
    ro.add_argument("--limit", type=int, default=25, help="How many emails to fetch (default: 25)")
    sub.add_parser("get-updates", help="Print Telegram chat_id from recent bot updates")

    p_imap = sub.add_parser("imap-debug", help="Debug IMAP: counts and sample headers")
    p_imap.add_argument("--sample", type=int, default=10, help="How many latest messages to sample (default: 10)")

    p_reset = sub.add_parser("reset-db", help="Delete SQLite DB (SQLITE_PATH) to reset SOCANA state")
    p_reset.add_argument("--yes", action="store_true", help="Confirm deletion (required)")

    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.cmd == "get-updates":
        raise SystemExit(asyncio.run(cmd_get_updates()))

    if args.cmd == "imap-debug":
        raise SystemExit(asyncio.run(cmd_imap_debug(sample=args.sample)))

    if args.cmd == "reset-db":
        raise SystemExit(cmd_reset_db(yes=getattr(args, "yes", False)))

    from soc_core.app import run, run_once
    from soc_core.config import load_settings

    settings = load_settings()

    if args.cmd == "run":
        asyncio.run(run(settings))
    elif args.cmd == "run-once":
        asyncio.run(run_once(settings, mode=getattr(args, "mode", "unseen"), limit=getattr(args, "limit", 25)))
    else:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
