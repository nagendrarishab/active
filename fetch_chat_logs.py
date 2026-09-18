#!/opt/anaconda3/bin/python3
"""Pull new Vault Events Bot messages from the Google Chat space, parse them,
and push the derived rows into Sheet1 -- incrementally, so a re-run (e.g. from
automate.py) never reprocesses a day it has already synced.

Progress is tracked in chat_sync_state.json:
    - last_message_time: only messages posted after this are fetched next run
                         (kept in exact RFC3339 UTC since that's what the
                         Chat API filter requires -- see
                         last_message_time_readable for a human-readable copy)
    - last_message_time_readable: the same instant, formatted for humans
                         reading the state file -- not used by the code
    - session_counters:  per-date-per-branch session numbering (S1, S2, ...),
                          continued across runs. A "session" is a burst of
                          activity separated by a real time gap (see
                          parse_vault_logs.SESSION_GAP), not a Chat message
                          boundary -- the bot can post many small messages
                          within one continuous burst.
    - last_known_tray_state: each branch's last known (tray, state), carried
                          forward so a run whose fetched batch doesn't
                          include the STATE_TRANSITION that set up the
                          current tray still classifies detections correctly

Requires (in .env): CHAT_CLIENT_ID, CHAT_CLIENT_SECRET, VAULT_CHAT_SPACE
Requires chat_token.json (from a one-time `python3 chat_auth_setup.py` run).

Usage:
    python3 fetch_chat_logs.py
"""
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import parse_vault_logs as pv

IST = ZoneInfo("Asia/Kolkata")


def readable_ist(iso_utc: str) -> str:
    """RFC3339 UTC (e.g. from the Chat API's createTime) -> human string in
    IST, matching the log lines' own timestamp style, e.g.
    "17 Sep 2026, 06:44:07 PM IST"."""
    ts = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(IST)
    return ts.strftime("%d %b %Y, %I:%M:%S %p IST")

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN_PATH = ROOT / "chat_token.json"
STATE_PATH = ROOT / "chat_sync_state.json"


def parse_space_id(raw: str) -> str:
    """Accepts a bare id, 'spaces/AAAA...', a chat.google.com/room/... link,
    or the chat.googleapis.com incoming-webhook URL -- and returns 'spaces/AAAA...'."""
    raw = raw.strip()
    m = re.search(r"spaces/([\w-]+)", raw)
    if m:
        return f"spaces/{m.group(1)}"
    m = re.search(r"room/([\w-]+)", raw)
    if m:
        return f"spaces/{m.group(1)}"
    return raw if raw.startswith("spaces/") else f"spaces/{raw}"


def get_access_token():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"{TOKEN_PATH} not found -- run `python3 chat_auth_setup.py` once first."
        )
    client_id = os.environ.get("CHAT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("CHAT_CLIENT_SECRET", "").strip()
    refresh_token = json.loads(TOKEN_PATH.read_text())["refresh_token"]

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def load_state():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
        state.setdefault("last_known_tray_state", {})
        return state
    return {"last_message_time": None, "session_counters": {}, "last_known_tray_state": {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def fetch_new_messages(space_id, access_token, since_iso):
    messages = []
    page_token = None
    while True:
        params = {"pageSize": 1000, "orderBy": "createTime asc"}
        if since_iso:
            params["filter"] = f'createTime > "{since_iso}"'
        if page_token:
            params["pageToken"] = page_token

        url = f"https://chat.googleapis.com/v1/{space_id}/messages?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        messages.extend(data.get("messages", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return messages


def main():
    space_raw = os.environ.get("VAULT_CHAT_SPACE", "").strip()
    if not space_raw:
        print("VAULT_CHAT_SPACE is not set in .env")
        return
    space_id = parse_space_id(space_raw)

    state = load_state()
    access_token = get_access_token()
    messages = fetch_new_messages(space_id, access_token, state["last_message_time"])

    if not messages:
        print("[Chat Sync] No new messages since last run.")
        return

    # parse_vault_logs derives sessions from time gaps between events, not
    # from Chat message boundaries (a single message doesn't correspond to
    # one meaningful session), so these just need to be concatenated.
    blob = "\n".join(m.get("text", "") for m in messages)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write(blob)
        tmp_path = tmp.name

    try:
        states, detections, opened_early, spans, session_counters = pv.load_events(
            [tmp_path], session_counters=state["session_counters"]
        )
    finally:
        os.unlink(tmp_path)

    seed_states = {branch: tuple(v) for branch, v in state["last_known_tray_state"].items()}
    summary = pv.summarize(states, detections, opened_early=opened_early, seed_states=seed_states)
    rows = pv.build_rows(summary, spans)

    print(f"[Chat Sync] {len(messages)} new message(s), {len(rows)} row(s) derived.")
    for date, session, branch, event_name, count, context, time_range in rows:
        print(f"  {date:<10}{session:<6}{branch:<12}{event_name:<28}{count:<6}{context}")

    pv.push_to_sheet(rows)

    state["last_message_time"] = messages[-1]["createTime"]
    state["last_message_time_readable"] = readable_ist(state["last_message_time"])
    state["session_counters"] = session_counters
    updated_tray_state = pv.latest_known_state(states, seed_states=seed_states)
    state["last_known_tray_state"] = {branch: list(v) for branch, v in updated_tray_state.items()}
    save_state(state)
    print(f"[Chat Sync] State saved -- last message time {state['last_message_time_readable']}")


if __name__ == "__main__":
    main()
