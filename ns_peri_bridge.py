#!/usr/bin/env python3
"""ns_peri_bridge.py: send pending NeoSapien reminders to their owner's own
WhatsApp DM (via Perisclaw), with interactive name approval.

This is deliberately separate from ns_sync.py: ns_sync.py only ever writes to
the self-chat. This script is the one that can message a real third party, so
it never sends automatically to a name it hasn't seen resolved in
contacts.json, and it never resends a reminder it already sent once.

Usage:
  python ns_peri_bridge.py --dry-run                 # show what would be sent
  python ns_peri_bridge.py --approve-names            # resolve new owner names interactively
  python ns_peri_bridge.py --send                     # actually send
  python ns_peri_bridge.py --send --include-inferred --include-stale --stale-days 14
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ns_sync import (
    MCPClient, load_env, load_contacts, clean, send_chunks, fetch_reminders,
    format_priority, SELF_NAMES, BLOCKED_OWNERS,
)

BASE_DIR = Path(__file__).resolve().parent
CONTACTS_PATH = BASE_DIR / "contacts.json"
SENT_STATE_PATH = BASE_DIR / "sent_reminders.json"


def save_contacts(contacts):
    CONTACTS_PATH.write_text(json.dumps(contacts, indent=2, ensure_ascii=False) + "\n")


def load_sent_state():
    if SENT_STATE_PATH.exists():
        return set(json.loads(SENT_STATE_PATH.read_text()))
    return set()


def save_sent_state(sent_ids):
    SENT_STATE_PATH.write_text(json.dumps(sorted(sent_ids)))


def group_by_owner(reminders):
    """Groups by lowercased owner name so 'Name' and 'name' become one
    recipient instead of two separate sends."""
    grouped = {}
    for r in reminders:
        if (r.get("status") or "").lower() != "pending":
            continue
        owner = (r.get("owner") or "").strip()
        if not owner:
            continue
        grouped.setdefault(owner.lower(), []).append(r)
    return grouped


def resolve_owner(owner, contacts):
    key = owner.strip().lower()
    if key in SELF_NAMES or key in BLOCKED_OWNERS:
        return "blocked"
    entry = contacts.get(key)
    if entry is None:
        return "unknown"
    if not entry.get("jid"):
        return "blocked"
    return entry


def approve_names(peri, contacts, grouped):
    unresolved = [o for o in grouped if resolve_owner(o, contacts) == "unknown"]
    if not unresolved:
        print("Sab owner names already resolved ya blocked hain.")
        return

    for owner in unresolved:
        print(f"\n=== {owner} ({len(grouped[owner])} pending tasks) ===")
        res = peri.call("search_contacts", {"query": owner, "limit": 8})
        candidates = res.get("contacts") or res.get("results") or [] if isinstance(res, dict) else []
        if not candidates:
            print("Koi WhatsApp contact nahi mila.")
        for i, c in enumerate(candidates, 1):
            name = c.get("name") or c.get("pushname") or "(no name)"
            jid = c.get("jid") or c.get("id") or c.get("contact_id")
            print(f"  {i}. {name}  <{jid}>")
        print("  0. Block (kabhi mat bhejo)")
        print("  s. Skip (baad me decide karenge)")

        choice = input("Pick number, 0, or s: ").strip().lower()
        key = owner.strip().lower()
        if choice in ("s", ""):
            continue
        if choice == "0":
            contacts[key] = {"jid": None, "label": f"BLOCKED - manual, {owner}"}
        else:
            try:
                c = candidates[int(choice) - 1]
            except (ValueError, IndexError):
                print("Invalid, skip kar diya.")
                continue
            contacts[key] = {
                "jid": c.get("jid") or c.get("id") or c.get("contact_id"),
                "label": c.get("name") or c.get("pushname") or owner,
            }
        save_contacts(contacts)
        print(f"Saved: {owner} -> {contacts[key]}")


def build_task_message(tasks):
    lines = ["Hi! Ye aapke pending tasks hain:\n"]
    for r in tasks:
        name = r.get("task_name") or "(untitled task)"
        details = r.get("details") or ""
        due = r.get("due_date")
        due_str = f" (due {due[:10]})" if due else ""
        lines.append(f"- {format_priority(r.get('priority'))} *{name}*{due_str}")
        if details:
            lines.append(f"  {details}")
    return clean("\n".join(lines))


def do_send(peri, contacts, grouped, sent_ids, include_inferred, include_stale,
            stale_days, dry_run):
    now = datetime.now(timezone.utc)
    for owner, tasks in grouped.items():
        resolved = resolve_owner(owner, contacts)
        if resolved in ("blocked", "unknown"):
            continue

        pending_tasks = []
        for r in tasks:
            if r.get("id") in sent_ids:
                continue
            if not include_inferred and r.get("who_inferred") and r.get("due_inferred"):
                continue
            due = r.get("due_date")
            if due and not include_stale:
                try:
                    due_dt = datetime.fromisoformat(due)
                    if due_dt.tzinfo is None:
                        due_dt = due_dt.replace(tzinfo=timezone.utc)
                    if (now - due_dt).days > stale_days:
                        continue
                except ValueError:
                    pass
            pending_tasks.append(r)

        if not pending_tasks:
            continue

        msg = build_task_message(pending_tasks)
        print(f"--> {owner} [{resolved['label']}] <{resolved['jid']}>: {len(pending_tasks)} task(s)")
        if dry_run:
            print(msg)
            continue

        send_chunks(peri, resolved["jid"], msg)
        for r in pending_tasks:
            sent_ids.add(r["id"])
        save_sent_state(sent_ids)
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="Send pending reminders to their owners on WhatsApp")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approve-names", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--include-inferred", action="store_true",
                         help="Bhi bhejo jinme owner AUR due date dono AI-guessed hain")
    parser.add_argument("--include-stale", action="store_true",
                         help="Purane due-date wale tasks bhi bhejo")
    parser.add_argument("--stale-days", type=int, default=14)
    args = parser.parse_args()

    if not (args.dry_run or args.approve_names or args.send):
        parser.error("Ek mode chuno: --dry-run, --approve-names, ya --send")

    env = load_env()
    peri = MCPClient(env["PERISCLAW_MCP_URL"], env["PERISCLAW_TOKEN"], "perisclaw")
    ns = MCPClient(env["NEOSAPIEN_MCP_URL"], env["NEOSAPIEN_TOKEN"], "neosapien")

    contacts = load_contacts()
    grouped = group_by_owner(fetch_reminders(ns))
    sent_ids = load_sent_state()

    if args.approve_names:
        approve_names(peri, contacts, grouped)
        return

    do_send(peri, contacts, grouped, sent_ids,
            include_inferred=args.include_inferred,
            include_stale=args.include_stale,
            stale_days=args.stale_days,
            dry_run=args.dry_run or not args.send)


if __name__ == "__main__":
    main()
