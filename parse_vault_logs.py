#!/opt/anaconda3/bin/python3
"""Parse Vault Events Bot log exports (from the Google Chat space) and fill in
the columns of the tray-event tracking sheet that can be derived from the logs
alone: Date, Session, Branch, production Event count, Production Context.

A "session" is one Vault Events Bot chat message -- Google Chat inserts a
"Vault Events Bot, App, <time>" line between separate messages when you copy
a thread, so those lines mark natural session boundaries. Everything before
the first such marker is session 1.

Tray state (IDLE / TRAY_PICKED / TRAY_IN_TRANSIT / TRAY_ON_TABLE) is tracked
globally across the whole file regardless of session, since it reflects the
real world, not the chat's message grouping -- only event *counts* are
bucketed per session.

The remaining columns (Training data/Event count/Context, Accuracy rate, FP,
FN) need a human to compare these logged detections against a manual review
of the footage -- the logs are the model's own output, not ground truth, so
correctness can't be self-graded from them.

Usage:
    python3 parse_vault_logs.py <log_file> [log_file ...]           # print only
    python3 parse_vault_logs.py <log_file> [log_file ...] --push    # also write
                                                                     # into the
                                                                     # Sheet1 tab
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

SESSION_MARKER_RE = re.compile(r"^Vault Events Bot,\s*App,")
STATE_RE = re.compile(
    r"\[STATE_TRANSITION\] (?P<branch>\S+) tray=(?P<tray>\S+) "
    r"old_state=(?P<old>\S+) new_state=(?P<new>\S+) @ (?P<ts>.+?) IST"
)
DETECT_RE = re.compile(
    r"\[OPEN_CLOSE_DETECTION\] (?P<branch>\S+) label=(?P<label>\S+)"
    r"(?: confidence=\d+% box=\([^)]+\))? @ (?P<ts>.+?) IST"
)
# Matches any bracketed log line (including WARNING_* ones), used only to
# widen the session's time span -- not for event counting.
ANY_LINE_RE = re.compile(r"^\[[A-Z_]+\] (?P<branch>\S+) .* @ (?P<ts>.+?) IST")
TS_FMT = "%d %b %Y, %I:%M:%S %p"


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FMT)


def load_events(paths, session_counters=None):
    """session_counters: {date_iso_str: last session number used that date},
    mutated in place and also returned -- pass in the persisted state from a
    prior run to keep numbering (S8, S9, ...) continuing correctly instead of
    restarting at S1, and to get correct per-day numbering even when a single
    run's input spans a date rollover. Defaults to a fresh {} (manual/CLI use
    starts each date back at S1, matching this script's original behavior)."""
    if session_counters is None:
        session_counters = {}

    states = defaultdict(list)      # branch -> [(ts, tray, old, new, session)]
    detections = defaultdict(list)  # branch -> [(ts, label, session)]
    spans = defaultdict(lambda: [None, None])  # (date, branch, session) -> [min_ts, max_ts]

    pending_new_session = True  # the very first chunk is also a boundary
    current_session, current_date = None, None

    for path in paths:
        with open(path) as f:
            for line in f:
                if SESSION_MARKER_RE.match(line):
                    pending_new_session = True
                    continue

                m_any = ANY_LINE_RE.match(line)
                if not m_any:
                    continue
                ts = parse_ts(m_any["ts"])
                date_key = ts.date().isoformat()

                if pending_new_session or date_key != current_date:
                    current_date = date_key
                    session_counters[date_key] = session_counters.get(date_key, 0) + 1
                    current_session = session_counters[date_key]
                    pending_new_session = False

                key = (ts.date(), m_any["branch"], current_session)
                lo, hi = spans[key]
                if lo is None or ts < lo:
                    spans[key][0] = ts
                if hi is None or ts > hi:
                    spans[key][1] = ts

                m = STATE_RE.search(line)
                if m:
                    states[m["branch"]].append(
                        (parse_ts(m["ts"]), m["tray"], m["old"], m["new"], current_session)
                    )
                    continue
                m = DETECT_RE.search(line)
                if m and m["label"] != "None":
                    detections[m["branch"]].append((parse_ts(m["ts"]), m["label"], current_session))

    for branch in states:
        states[branch].sort(key=lambda r: r[0])
    for branch in detections:
        detections[branch].sort(key=lambda r: r[0])
    return states, detections, spans, session_counters


def tray_at(states_for_branch, ts):
    """Most recent (tray, state) as of ts, or (None, "IDLE") if none yet.
    Uses the full chronological state history regardless of session."""
    tray, state = None, "IDLE"
    for row_ts, row_tray, _old, new, _session in states_for_branch:
        if row_ts > ts:
            break
        tray, state = row_tray, new
    return tray, state


def summarize(states, detections):
    """(date, branch, session) -> event_name -> {count, context: set(tray)}"""
    out = defaultdict(lambda: defaultdict(lambda: {"count": 0, "context": set()}))

    for branch, rows in states.items():
        for ts, tray, old, _new, session in rows:
            if old == "IDLE":
                key = (ts.date(), branch, session)
                out[key]["Box came out of safe"]["count"] += 1
                out[key]["Box came out of safe"]["context"].add(tray)

    for branch, rows in detections.items():
        branch_states = states.get(branch, [])
        for ts, label, session in rows:
            key = (ts.date(), branch, session)
            tray, state = tray_at(branch_states, ts)
            if label == "open":
                out[key]["Box opened(in frames)"]["count"] += 1
                out[key]["Box opened(in frames)"]["context"].add(tray)
                if state != "TRAY_ON_TABLE":
                    out[key]["Box opened outside of table"]["count"] += 1
                    out[key]["Box opened outside of table"]["context"].add(tray)
            elif label == "closed":
                out[key]["Box closed(in frames)"]["count"] += 1
                out[key]["Box closed(in frames)"]["context"].add(tray)

    return out


EVENT_ORDER = [
    "Box came out of safe",
    "Box opened outside of table",
    "Box opened(in frames)",
    "Box closed(in frames)",
]

# Column order of the tab the user created inside report's Google Sheet.
SHEET_COLUMNS = [
    "Date", "Session", "Event name", "Training data", "Training Event count",
    "Training Context", "Production data", "production Event count",
    "Production Context", "Accuracy rate", "FP", "FN", "Branch",
    "Session Time Range",
]
PROD_COUNT_COL = SHEET_COLUMNS.index("production Event count")   # H
PROD_CONTEXT_COL = SHEET_COLUMNS.index("Production Context")     # I
BRANCH_COL = SHEET_COLUMNS.index("Branch")                        # M
TIME_RANGE_COL = SHEET_COLUMNS.index("Session Time Range")        # N


def format_time_range(span):
    lo, hi = span
    if lo is None:
        return ""
    return f"{lo.strftime('%-I:%M %p')} - {hi.strftime('%-I:%M %p')}"


def build_rows(summary, spans):
    """summary -> ordered list of
    (date, session, branch, event_name, count, context_str, time_range)."""
    rows = []
    for (date, branch, session) in sorted(summary, key=lambda k: (k[0], k[2])):
        events = summary[(date, branch, session)]
        time_range = format_time_range(spans.get((date, branch, session), (None, None)))
        for event_name in EVENT_ORDER:
            if event_name not in events:
                continue
            data = events[event_name]
            context = ", ".join(sorted(t for t in data["context"] if t))
            rows.append((date.strftime("%-d %b"), f"S{session}", branch, event_name,
                         data["count"], context, time_range))
    return rows


def col_letter(idx):
    return chr(ord("A") + idx)


def fetch_sheet_rows(sheet_id, tab_name, access_token):
    range_name = urllib.parse.quote(f"{tab_name}!A:N")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("values", [])


def index_existing_rows(sheet_rows):
    """Forward-fills blank Date/Session cells (the sheet leaves them blank on
    continuation rows of the same session) and returns
    {(date, session, event_name): (sheet_row_number, row, is_head)}, where
    is_head means this row physically carries the Date value (the first row
    of its session group) -- that's the only row the time range belongs on."""
    index = {}
    last_date, last_session = "", ""
    for i, row in enumerate(sheet_rows[1:], start=2):  # skip header, sheet rows are 1-indexed
        is_head = bool(len(row) > 0 and row[0] and row[0].strip())
        date = (row[0].strip() if is_head else "") or last_date
        session = (row[1].strip() if len(row) > 1 and row[1] else "") or last_session
        last_date, last_session = date, session
        event_name = row[2].strip() if len(row) > 2 and row[2] else ""
        if event_name:
            index[(date, session, event_name)] = (i, row, is_head)
    return index


def get_grid_id(sheet_id, tab_name, access_token):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for s in data.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab_name}' not found in spreadsheet {sheet_id}")


def reset_number_format(sheet_id, access_token, grid_id, row_num, col_idx):
    """Some rows in this sheet carry a leftover Percent format from earlier
    manual edits -- writing a plain count like 2 into one renders as "200%"
    even though the stored value is correct. Force plain-number formatting on
    the cell we're about to write a count into so that can't happen."""
    body = json.dumps({"requests": [{
        "repeatCell": {
            "range": {
                "sheetId": grid_id,
                "startRowIndex": row_num - 1, "endRowIndex": row_num,
                "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }]}).encode("utf-8")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def sheets_update(sheet_id, tab_name, access_token, range_suffix, values):
    range_name = urllib.parse.quote(f"{tab_name}!{range_suffix}")
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
           f"{range_name}?valueInputOption=USER_ENTERED")
    body = json.dumps({"values": [values]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def sheets_append(sheet_id, tab_name, access_token, rows):
    """Returns the 1-indexed row number of the first newly-appended row."""
    range_name = urllib.parse.quote(f"{tab_name}!A:N")
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
           f"{range_name}:append?valueInputOption=USER_ENTERED")
    body = json.dumps({"values": rows}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    updated_range = data["updates"]["updatedRange"]  # e.g. "Sheet1!A6:N9"
    start_cell = updated_range.split("!")[1].split(":")[0]
    return int(re.search(r"\d+", start_cell).group())


def push_to_sheet(rows):
    """For each (date, session, event_name) row: if it already exists in the
    sheet, fill in production count/context/branch only where those cells are
    currently blank (never overwrite a value someone already filled in --
    same rule sync_sheets.py's --pull follows). Otherwise append a new row,
    putting Date/Session/Branch only on the first new row of each group to
    match the sheet's existing style of blank continuation rows."""
    from dotenv import load_dotenv
    import sync_sheets as ss

    load_dotenv(ss.ROOT / ".env")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    tab_name = os.environ.get("VAULT_EVENTS_TAB", "Sheet1").strip()
    if not sheet_id:
        print("GOOGLE_SHEET_ID is not set in .env")
        return

    access_token = ss.get_access_token()
    grid_id = get_grid_id(sheet_id, tab_name, access_token)
    sheet_rows = fetch_sheet_rows(sheet_id, tab_name, access_token)
    existing = index_existing_rows(sheet_rows) if sheet_rows else {}

    header = sheet_rows[0] if sheet_rows else []
    if len(header) <= TIME_RANGE_COL or not header[TIME_RANGE_COL].strip():
        sheets_update(sheet_id, tab_name, access_token,
                       f"{col_letter(TIME_RANGE_COL)}1", ["Session Time Range"])

    to_append = []
    updated, appended = 0, 0
    seen_group = set()
    for date, session, branch, event_name, count, context, time_range in rows:
        key = (date, session, event_name)
        if key in existing:
            row_num, existing_row, is_head = existing[key]
            existing_row += [""] * (len(SHEET_COLUMNS) - len(existing_row))
            if not existing_row[PROD_COUNT_COL] and not existing_row[PROD_CONTEXT_COL]:
                reset_number_format(sheet_id, access_token, grid_id, row_num, PROD_COUNT_COL)
                sheets_update(sheet_id, tab_name, access_token,
                               f"{col_letter(PROD_COUNT_COL)}{row_num}:{col_letter(PROD_CONTEXT_COL)}{row_num}",
                               [count, context])
                updated += 1
            if not existing_row[BRANCH_COL]:
                sheets_update(sheet_id, tab_name, access_token,
                               f"{col_letter(BRANCH_COL)}{row_num}", [branch])
            if is_head and time_range and not existing_row[TIME_RANGE_COL]:
                sheets_update(sheet_id, tab_name, access_token,
                               f"{col_letter(TIME_RANGE_COL)}{row_num}", [time_range])
        else:
            group = (date, session, branch)
            new_row = [""] * len(SHEET_COLUMNS)
            if group not in seen_group:
                new_row[0], new_row[1], new_row[BRANCH_COL] = date, session, branch
                new_row[TIME_RANGE_COL] = time_range
                seen_group.add(group)
            new_row[2] = event_name
            new_row[PROD_COUNT_COL] = count
            new_row[PROD_CONTEXT_COL] = context
            to_append.append(new_row)
            appended += 1

    if to_append:
        start_row = sheets_append(sheet_id, tab_name, access_token, to_append)
        for offset in range(len(to_append)):
            reset_number_format(sheet_id, access_token, grid_id, start_row + offset, PROD_COUNT_COL)

    print(f"[Vault Events Sync] Filled blanks on {updated} existing row(s), "
          f"appended {appended} new row(s) to '{tab_name}'.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    push = "--push" in sys.argv
    paths = [a for a in sys.argv[1:] if a != "--push"]

    states, detections, spans, _ = load_events(paths)
    summary = summarize(states, detections)
    rows = build_rows(summary, spans)

    print(f"{'Date':<10}{'Session':<10}{'Branch':<12}{'Event name':<28}{'Prod count':<12}"
          f"{'Production Context':<24}{'Time Range'}")
    for date, session, branch, event_name, count, context, time_range in rows:
        print(f"{date:<10}{session:<10}{branch:<12}{event_name:<28}{count:<12}"
              f"{context:<24}{time_range}")

    if push:
        push_to_sheet(rows)


if __name__ == "__main__":
    main()
