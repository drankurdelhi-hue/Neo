#!/usr/bin/env python3
"""ns-sync: NeoSapien -> ns_sync.py -> Perisclaw (WhatsApp) -> Perisclaw agent.

Two modes:
  --push {today,week,tasks,summary}   one-shot digest, sent to the WhatsApp self-chat
  --listen --poll SECONDS             daemon: watches self-chat for /ns commands

Perisclaw is the actual WhatsApp gateway: this script never talks to WhatsApp
directly, it calls Perisclaw's message_send / get_messages MCP tools.
"""
import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "sync_state.json"
CONTACTS_PATH = BASE_DIR / "contacts.json"

MAX_CHUNK = 4000
REQUIRED_KEYS = ("NEOSAPIEN_TOKEN", "PERISCLAW_TOKEN", "NEOSAPIEN_MCP_URL",
                 "PERISCLAW_MCP_URL", "SELF_JID")

# Owner values that are never a real recipient. The operator's own name goes in
# contacts.json as a jid: null entry, so it stays out of a public repo.
SELF_NAMES = {"unassigned", "self", ""}
# Role labels the AI extracted as "owner" that are not actual people.
BLOCKED_OWNERS = {"interior designer", "furniture vendor", "developer", "speaker 2"}

log = logging.getLogger("ns_sync")


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    for key in REQUIRED_KEYS:
        if os.environ.get(key):
            env[key] = os.environ[key]
    missing = [k for k in REQUIRED_KEYS if not env.get(k)]
    if missing:
        sys.exit(f"Missing config: {', '.join(missing)} (check .env)")
    return env


def load_contacts():
    if not CONTACTS_PATH.exists():
        return {}
    raw = json.loads(CONTACTS_PATH.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


class MCPClient:
    """Minimal streamable-HTTP MCP client: initialize -> session id -> tools/call.

    Handles both SSE (text/event-stream) and plain JSON responses, per the
    verified behaviour of the NeoSapien and Perisclaw MCP endpoints.
    """

    def __init__(self, url, token, name):
        self.url = url
        self.name = name
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        self.session_id = None
        self._initialize()

    def _post(self, payload):
        resp = self.http.post(self.url, json=payload, timeout=60)
        resp.raise_for_status()
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
            self.http.headers["Mcp-Session-Id"] = sid
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            return self._parse_sse(resp.text)
        if not resp.text.strip():
            return None
        return resp.json()

    @staticmethod
    def _parse_sse(text):
        data_lines = [line[len("data:"):].strip() for line in text.splitlines()
                      if line.startswith("data:")]
        if not data_lines:
            return None
        return json.loads("\n".join(data_lines))

    def _initialize(self):
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ns-sync", "version": "1.0"},
            },
        }
        result = self._post(payload)
        if result is None or "error" in result:
            raise RuntimeError(f"{self.name} MCP initialize failed: {result}")
        try:
            self.http.post(self.url, json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }, timeout=30)
        except requests.RequestException:
            pass

    def call(self, tool_name, arguments=None):
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }
        result = self._post(payload)
        if result is None:
            raise RuntimeError(f"{self.name}.{tool_name}: empty response")
        if "error" in result:
            raise RuntimeError(f"{self.name}.{tool_name}: {result['error']}")
        content = result.get("result", {}).get("content", [])
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        raw = "\n".join(text_parts) if text_parts else ""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw


def clean(text):
    """WhatsApp markup is not Markdown; the one hazard we normalize is em/en dash."""
    if not text:
        return ""
    return text.replace("—", "-").replace("–", "-")


def send_chunks(peri, chat_id, text):
    text = clean(text)
    if len(text) <= MAX_CHUNK:
        peri.call("message_send", {"chat_id": chat_id, "text": text})
        return
    parts = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
    total = len(parts)
    for i, part in enumerate(parts, 1):
        peri.call("message_send", {"chat_id": chat_id, "text": f"{part}\n\n({i}/{total})"})
        time.sleep(1)


def _extract_list(res, keys=("reminders", "memories", "results", "items", "data")):
    if not isinstance(res, dict):
        return []
    for key in keys:
        if isinstance(res.get(key), list):
            return res[key]
    return []


def unwrap_memory(item):
    return item.get("memory", item) if isinstance(item, dict) else item


def fetch_memories(ns, start_date, end_date, query=None, limit_per_page=100):
    memories = []
    page = 1
    while True:
        args = {
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "limit": limit_per_page,
            "sort_by": "created_at",
            "sort_order": "asc",
        }
        if query:
            args["query"] = query
        res = ns.call("search_memories", args)
        items = _extract_list(res)
        memories.extend(items)
        total_pages = res.get("total_pages", 1) if isinstance(res, dict) else 1
        if page >= total_pages or not items:
            break
        page += 1
    return memories


def fetch_reminders(ns):
    reminders = []
    page = 1
    while True:
        res = ns.call("get_reminders", {"page": page, "limit": 100})
        items = _extract_list(res)
        reminders.extend(items)
        pagination = res.get("pagination", {}) if isinstance(res, dict) else {}
        if not pagination.get("has_more"):
            break
        page += 1
    return reminders


def format_priority(p):
    return {"critical": "\U0001F534", "high": "\U0001F7E0",
            "medium": "\U0001F7E1", "low": "\U0001F7E2"}.get((p or "").lower(), "⚪")


def format_memory_line(item):
    m = unwrap_memory(item)
    title = m.get("title") or "(untitled)"
    summary = m.get("summary") or ""
    ts = m.get("created_at") or m.get("started_at") or ""
    date_str = ts[:10] if ts else ""
    line = f"*{title}*" + (f" ({date_str})" if date_str else "")
    if summary:
        line += f"\n{summary}"
    return line


def build_memory_digest(memories, heading):
    if not memories:
        return f"*{heading}*\n\nKoi memory nahi mili."
    lines = [f"*{heading}* - {len(memories)} memories\n"]
    for item in memories:
        lines.append(format_memory_line(item))
        lines.append("")
    return "\n".join(lines).strip()


def build_tasks_digest(reminders):
    pending = [r for r in reminders if (r.get("status") or "").lower() == "pending"]
    if not pending:
        return "*Pending Tasks*\n\nKoi pending reminder nahi hai."
    grouped = {}
    labels = {}
    for r in pending:
        owner = (r.get("owner") or "Unassigned").strip() or "Unassigned"
        key = owner.lower()
        labels.setdefault(key, owner)
        grouped.setdefault(key, []).append(r)

    lines = [f"*Pending Tasks* - {len(pending)} total\n"]
    for key in sorted(grouped, key=str.lower):
        items = grouped[key]
        lines.append(f"*{labels[key]}* ({len(items)})")
        for r in items:
            name = r.get("task_name") or "(untitled task)"
            due = r.get("due_date")
            due_str = f" - due {due[:10]}" if due else ""
            flags = []
            if r.get("who_inferred"):
                flags.append("owner guessed")
            if r.get("due_inferred"):
                flags.append("due guessed")
            flag_str = f" _({', '.join(flags)})_" if flags else ""
            lines.append(f"- {format_priority(r.get('priority'))} {name}{due_str}{flag_str}")
        lines.append("")
    return "\n".join(lines).strip()


def build_summary_digest(summary):
    if not summary:
        return "*Daily Summary*\n\nKoi summary nahi mili."
    lines = ["*Daily Summary*" + (f" - {summary.get('date')}" if summary.get("date") else "")]
    for key, label in (
        ("overview", "Overview"), ("summary", "Overview"),
        ("decisions", "Decisions"), ("open_points", "Open Points"),
        ("learnings", "Learnings"), ("reminders", "Reminders"),
    ):
        val = summary.get(key)
        if not val:
            continue
        lines.append(f"\n*{label}*")
        if isinstance(val, list):
            for v in val:
                if isinstance(v, dict):
                    v = v.get("text") or v.get("task_name") or json.dumps(v)
                lines.append(f"- {v}")
        else:
            lines.append(str(val))
    return "\n".join(lines).strip()


def day_bounds(dt):
    return dt.strftime("%Y-%m-%dT00:00:00"), dt.strftime("%Y-%m-%dT23:59:59")


def do_push(mode):
    env = load_env()
    ns = MCPClient(env["NEOSAPIEN_MCP_URL"], env["NEOSAPIEN_TOKEN"], "neosapien")
    peri = MCPClient(env["PERISCLAW_MCP_URL"], env["PERISCLAW_TOKEN"], "perisclaw")

    if mode == "today":
        start, end = day_bounds(datetime.now().astimezone())
        digest = build_memory_digest(fetch_memories(ns, start, end), "Aaj ki Memories")
    elif mode == "week":
        now = datetime.now().astimezone()
        start, _ = day_bounds(now - timedelta(days=7))
        _, end = day_bounds(now)
        digest = build_memory_digest(fetch_memories(ns, start, end), "Pichle 7 din ki Memories")
    elif mode == "tasks":
        digest = build_tasks_digest(fetch_reminders(ns))
    elif mode == "summary":
        digest = build_summary_digest(ns.call("get_daily_summary", {}))
    else:
        sys.exit(f"Unknown push mode: {mode}")

    send_chunks(peri, "self", digest)
    log.info("Pushed %s (%d chars)", mode, len(digest))


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_seen_ts": None}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state))


def _parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def handle_command(ns, peri, body):
    body = (body or "").strip()
    if not body.lower().startswith("/ns"):
        return
    parts = body[3:].strip().split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    log.info("Command: /ns %s %s", cmd, arg)

    try:
        if cmd == "today":
            start, end = day_bounds(datetime.now().astimezone())
            digest = build_memory_digest(fetch_memories(ns, start, end), "Aaj ki Memories")
        elif cmd == "week":
            now = datetime.now().astimezone()
            start, _ = day_bounds(now - timedelta(days=7))
            _, end = day_bounds(now)
            digest = build_memory_digest(fetch_memories(ns, start, end), "Pichle 7 din ki Memories")
        elif cmd == "tasks":
            digest = build_tasks_digest(fetch_reminders(ns))
        elif cmd == "summary":
            digest = build_summary_digest(ns.call("get_daily_summary", {}))
        elif cmd == "search":
            query = arg.strip()
            if not query:
                digest = "Search query do: /ns search <kuch text>"
            else:
                res = ns.call("search_memories", {"query": query, "limit": 10})
                digest = build_memory_digest(_extract_list(res), f'Search: "{query}"')
        else:
            digest = ("Samajh nahi aaya. Commands:\n"
                      "/ns today\n/ns week\n/ns tasks\n/ns summary\n/ns search <query>")
    except Exception as e:
        log.exception("command failed")
        digest = f"Error: {e}"

    send_chunks(peri, "self", digest)


def poll_once(ns, peri, self_jid, state):
    res = peri.call("get_messages", {"chat_id": self_jid, "limit": 20})
    messages = res.get("messages", []) if isinstance(res, dict) else []

    last_seen = state.get("last_seen_ts")
    last_dt = _parse_ts(last_seen) if last_seen else None
    newest_ts = last_seen

    human_msgs = []
    for msg in sorted(messages, key=lambda m: m.get("timestamp") or ""):
        ts = msg.get("timestamp")
        if not ts:
            continue
        ts_dt = _parse_ts(ts)
        if last_dt and ts_dt <= last_dt:
            continue
        newest_ts = ts
        # performed_by null/empty = a human typed it on the phone; anything
        # else (perisclaw, this script) must be skipped or we loop forever.
        if not msg.get("performed_by"):
            human_msgs.append(msg)

    for msg in human_msgs:
        handle_command(ns, peri, msg.get("body") or "")

    if newest_ts and newest_ts != last_seen:
        state["last_seen_ts"] = newest_ts
        save_state(state)


def do_listen(poll_interval):
    env = load_env()
    ns = MCPClient(env["NEOSAPIEN_MCP_URL"], env["NEOSAPIEN_TOKEN"], "neosapien")
    peri = MCPClient(env["PERISCLAW_MCP_URL"], env["PERISCLAW_TOKEN"], "perisclaw")
    self_jid = env["SELF_JID"]
    state = load_state()
    log.info("Listening on %s every %ss", self_jid, poll_interval)

    while True:
        try:
            poll_once(ns, peri, self_jid, state)
        except Exception:
            log.exception("poll iteration failed")
        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="NeoSapien -> Perisclaw -> WhatsApp bridge")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--push", choices=["today", "week", "tasks", "summary"])
    group.add_argument("--listen", action="store_true")
    parser.add_argument("--poll", type=int, default=20, help="Poll interval in seconds for --listen")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                         stream=sys.stdout)

    if args.push:
        do_push(args.push)
    else:
        do_listen(args.poll)


if __name__ == "__main__":
    main()
