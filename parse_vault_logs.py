#!/opt/anaconda3/bin/python3
"""Parse Vault Events Bot log exports (from the Google Chat space) and fill in
the columns of the tray-event tracking sheet that can be derived from the logs
alone: Date, Session, Branch, production Event count, Production Context.

A "session" is a burst of activity for one branch with no gap longer than
SESSION_GAP between consecutive events -- NOT one Chat message. The bot can
post many separate tiny messages within a single continuous burst of tray
activity, so message boundaries don't reflect anything meaningful; a real
pause in activity does.

Tray state (IDLE / TRAY_PICKED / TRAY_IN_TRANSIT / TRAY_ON_TABLE) is tracked
globally across the whole file regardless of session, since it reflects the
real world, not this grouping -- only event *counts* are bucketed per
session.

The remaining columns (Training data/Event count/Context, Accuracy rate, FP,
FN) need a human to compare these logged detections against a manual review
of the footage -- the logs are the model's own output, not ground truth, so
correctness can't be self-graded from them.

Usage:
    python3 parse_vault_logs.py <log_file> [log_file ...]                 # print only
    python3 parse_vault_logs.py <log_file> [log_file ...] --push          # also write
                                                                           # into Sheet1
    python3 parse_vault_logs.py <log_file> [log_file ...] --date YYYY-MM-DD --push
                                                                           # only that date
    python3 parse_vault_logs.py --pull   # mirror Sheet1's current state into
                                          # report.xlsx's "Vault Events" tab
    python3 parse_vault_logs.py --summary  # rebuild a "Summary" tab (one row
                                            # per date+branch, event totals)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

CHUNK_SIZE = 300  # keep any single batch request comfortably under API size limits


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def request_json(url, access_token, method="GET", body=None, max_retries=6):
    """A thin urlopen wrapper with exponential backoff on 429/5xx -- needed
    once row counts get into the hundreds/thousands, where even a handful of
    calls can occasionally get rate-limited."""
    headers = {"Authorization": f"Bearer {access_token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    delay = 1
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 503) and attempt < max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise

SESSION_GAP = timedelta(minutes=5)
STATE_RE = re.compile(
    r"\[STATE_TRANSITION\] (?P<branch>\S+) tray=(?P<tray>\S+) "
    r"old_state=(?P<old>\S+) new_state=(?P<new>\S+) @ (?P<ts>.+?) IST"
)
DETECT_RE = re.compile(
    r"\[OPEN_CLOSE_DETECTION\] (?P<branch>\S+) label=(?P<label>\S+)"
    r"(?: confidence=\d+% box=\([^)]+\))? @ (?P<ts>.+?) IST"
)
# The system publishes ALERT_OPENED_EARLY itself whenever YOLO reports "open"
# while the tray's state isn't TRAY_ON_TABLE -- i.e. exactly what "Box opened
# outside of table" was previously *inferring* from tray_at(). No real sample
# line has been seen yet, so this tolerates an optional tray= field and any
# other fields between branch and the timestamp.
ALERT_OPENED_EARLY_RE = re.compile(
    r"\[ALERT_OPENED_EARLY\] (?P<branch>\S+)(?: tray=(?P<tray>\S+))?.*? @ (?P<ts>.+?) IST"
)
# Matches any bracketed log line (including WARNING_* ones), used only to
# widen the session's time span -- not for event counting.
ANY_LINE_RE = re.compile(r"^\[[A-Z_]+\] (?P<branch>\S+) .* @ (?P<ts>.+?) IST")
TS_FMT = "%d %b %Y, %I:%M:%S %p"


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FMT)


def load_events(paths, session_counters=None):
    """session_counters: {"<date_iso>|<branch>": last session number used},
    mutated in place and also returned -- pass in the persisted state from a
    prior run to keep numbering (S8, S9, ...) continuing correctly instead of
    restarting at S1. Defaults to a fresh {} (manual/CLI use starts each
    date+branch back at S1, matching this script's original behavior).

    A new session starts for a branch when: this is its first event seen,
    the date has rolled over, or more than SESSION_GAP has passed since its
    last event -- tracked independently per branch, since one branch's idle
    stretch shouldn't be affected by another branch's activity interleaved
    in the same input."""
    if session_counters is None:
        session_counters = {}

    states = defaultdict(list)       # branch -> [(ts, tray, old, new, session)]
    detections = defaultdict(list)   # branch -> [(ts, label, session)]
    opened_early = defaultdict(list)  # branch -> [(ts, tray, session)]
    spans = defaultdict(lambda: [None, None])  # (date, branch, session) -> [min_ts, max_ts]

    last_ts = {}          # branch -> last event ts seen
    current_session = {}  # branch -> current session number

    for path in paths:
        with open(path) as f:
            for line in f:
                m_any = ANY_LINE_RE.match(line)
                if not m_any:
                    continue
                ts = parse_ts(m_any["ts"])
                branch = m_any["branch"]
                date_key = ts.date().isoformat()

                prev_ts = last_ts.get(branch)
                if prev_ts is None or prev_ts.date() != ts.date() or (ts - prev_ts) > SESSION_GAP:
                    counter_key = f"{date_key}|{branch}"
                    session_counters[counter_key] = session_counters.get(counter_key, 0) + 1
                    current_session[branch] = session_counters[counter_key]
                last_ts[branch] = ts
                session = current_session[branch]

                key = (ts.date(), branch, session)
                lo, hi = spans[key]
                if lo is None or ts < lo:
                    spans[key][0] = ts
                if hi is None or ts > hi:
                    spans[key][1] = ts

                m = STATE_RE.search(line)
                if m:
                    states[branch].append((ts, m["tray"], m["old"], m["new"], session))
                    continue
                m = ALERT_OPENED_EARLY_RE.search(line)
                if m:
                    opened_early[branch].append((ts, m["tray"], session))
                    continue
                m = DETECT_RE.search(line)
                if m and m["label"] != "None":
                    detections[branch].append((ts, m["label"], session))

    for branch in states:
        states[branch].sort(key=lambda r: r[0])
    for branch in detections:
        detections[branch].sort(key=lambda r: r[0])
    for branch in opened_early:
        opened_early[branch].sort(key=lambda r: r[0])
    return states, detections, opened_early, spans, session_counters


def tray_at(states_for_branch, ts, seed=(None, "IDLE")):
    """Most recent (tray, state) as of ts, or `seed` if states_for_branch has
    nothing before ts. `seed` should be the last known (tray, state) carried
    over from a prior incremental run -- without it, a run whose fetched
    batch doesn't happen to include the STATE_TRANSITION that set up the
    currently active tray has no way to know the tray isn't still IDLE, and
    misclassifies every detection in that gap as "outside of table"."""
    tray, state = seed
    for row_ts, row_tray, _old, new, _session in states_for_branch:
        if row_ts > ts:
            break
        tray, state = row_tray, new
    return tray, state


def summarize(states, detections, opened_early=None, seed_states=None):
    """(date, branch, session) -> event_name -> {count, context: set(tray)}.
    seed_states: {branch: (tray, state)} carried over from a prior run (see
    tray_at) -- defaults to (None, "IDLE") per branch if not given.

    "Box opened outside of table" is counted only from real ALERT_OPENED_EARLY
    lines -- the system's own event for this exact condition. There is no
    inferred fallback: if a run's input has no such lines, this event simply
    doesn't appear for that run rather than guessing from tray_at() state."""
    seed_states = seed_states or {}
    opened_early = opened_early or {}
    out = defaultdict(lambda: defaultdict(lambda: {"count": 0, "context": set()}))

    for branch, rows in states.items():
        for ts, tray, old, _new, session in rows:
            if old == "IDLE":
                key = (ts.date(), branch, session)
                out[key]["Box came out of safe"]["count"] += 1
                out[key]["Box came out of safe"]["context"].add(tray)

    for branch, rows in detections.items():
        branch_states = states.get(branch, [])
        seed = seed_states.get(branch, (None, "IDLE"))
        for ts, label, session in rows:
            key = (ts.date(), branch, session)
            tray, _state = tray_at(branch_states, ts, seed=seed)
            if label == "open":
                out[key]["Box opened(in frames)"]["count"] += 1
                out[key]["Box opened(in frames)"]["context"].add(tray)
            elif label == "closed":
                out[key]["Box closed(in frames)"]["count"] += 1
                out[key]["Box closed(in frames)"]["context"].add(tray)

    for branch, rows in opened_early.items():
        for ts, tray, session in rows:
            key = (ts.date(), branch, session)
            out[key]["Box opened outside of table"]["count"] += 1
            out[key]["Box opened outside of table"]["context"].add(tray)

    return out


def latest_known_state(states, seed_states=None):
    """The (tray, state) each branch ends this run's batch in, for the
    caller to persist and pass back in as `seed_states` next run. Branches
    with no new STATE_TRANSITION this run keep their carried-over seed."""
    result = dict(seed_states or {})
    for branch, rows in states.items():
        if rows:
            _ts, tray, _old, new, _session = rows[-1]  # rows sorted ascending by ts
            result[branch] = (tray, new)
    return result


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
    (date, session, branch, event_name, count, context_str, time_range).

    Every event in EVENT_ORDER gets a row for every session, even ones with
    count=0 -- a human reviewing the actual footage may find the event
    happened even though it wasn't logged (or vice versa), so an absent row
    would hide that discrepancy instead of surfacing it for review."""
    rows = []
    for (date, branch, session) in sorted(summary, key=lambda k: (k[0], k[2])):
        events = summary[(date, branch, session)]
        time_range = format_time_range(spans.get((date, branch, session), (None, None)))
        for event_name in EVENT_ORDER:
            data = events.get(event_name, {"count": 0, "context": set()})
            context = ", ".join(sorted(t for t in data["context"] if t))
            rows.append((date.strftime("%-d %b"), f"S{session}", branch, event_name,
                         data["count"], context, time_range))
    return rows


def col_letter(idx):
    return chr(ord("A") + idx)


def fetch_sheet_rows(sheet_id, tab_name, access_token):
    range_name = urllib.parse.quote(f"{tab_name}!A:N")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    return request_json(url, access_token).get("values", [])


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
    data = request_json(url, access_token)
    for s in data.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab_name}' not found in spreadsheet {sheet_id}")


def format_reset_request(grid_id, row_num, col_idx, num_rows=1):
    """Some rows in this sheet carry a leftover Percent format from earlier
    manual edits -- writing a plain count like 2 into one renders as "200%"
    even though the stored value is correct. Building these as request dicts
    (rather than firing one API call each) lets the caller batch hundreds of
    them into a single batchUpdate call."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": grid_id,
                "startRowIndex": row_num - 1, "endRowIndex": row_num - 1 + num_rows,
                "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def apply_format_requests(sheet_id, access_token, requests):
    """Sends `requests` (repeatCell dicts) in chunks of CHUNK_SIZE, each as
    one batchUpdate call, instead of one HTTP call per request."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    for chunk in chunked(requests, CHUNK_SIZE):
        request_json(url, access_token, method="POST", body={"requests": chunk})


def apply_value_updates(sheet_id, tab_name, access_token, updates):
    """updates: list of (range_suffix, values) pairs. Sends them in chunks of
    CHUNK_SIZE via values:batchUpdate, instead of one PUT per cell/range."""
    if not updates:
        return
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchUpdate"
    for chunk in chunked(updates, CHUNK_SIZE):
        body = {
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"{tab_name}!{suffix}", "values": [values]} for suffix, values in chunk],
        }
        request_json(url, access_token, method="POST", body=body)


def sheets_append(sheet_id, tab_name, access_token, rows):
    """Returns the 1-indexed row number of the first newly-appended row.
    Appends in chunks so one giant payload can't trip request-size limits;
    each chunk still lands contiguously since Sheets appends in order."""
    range_name = urllib.parse.quote(f"{tab_name}!A:N")
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
           f"{range_name}:append?valueInputOption=USER_ENTERED")

    first_start_row = None
    for chunk in chunked(rows, CHUNK_SIZE):
        data = request_json(url, access_token, method="POST", body={"values": chunk})
        updated_range = data["updates"]["updatedRange"]  # e.g. "Sheet1!A6:N9"
        start_cell = updated_range.split("!")[1].split(":")[0]
        start_row = int(re.search(r"\d+", start_cell).group())
        if first_start_row is None:
            first_start_row = start_row
    return first_start_row


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

    value_updates = []   # (range_suffix, [values]) pairs -> one batchUpdate call
    format_requests = []  # repeatCell dicts -> one batchUpdate call

    header = sheet_rows[0] if sheet_rows else []
    if len(header) <= TIME_RANGE_COL or not header[TIME_RANGE_COL].strip():
        value_updates.append((f"{col_letter(TIME_RANGE_COL)}1", ["Session Time Range"]))

    to_append = []
    updated, appended = 0, 0
    seen_group = set()
    for date, session, branch, event_name, count, context, time_range in rows:
        key = (date, session, event_name)
        if key in existing:
            row_num, existing_row, is_head = existing[key]
            existing_row += [""] * (len(SHEET_COLUMNS) - len(existing_row))
            if not existing_row[PROD_COUNT_COL] and not existing_row[PROD_CONTEXT_COL]:
                format_requests.append(format_reset_request(grid_id, row_num, PROD_COUNT_COL))
                value_updates.append((
                    f"{col_letter(PROD_COUNT_COL)}{row_num}:{col_letter(PROD_CONTEXT_COL)}{row_num}",
                    [count, context],
                ))
                updated += 1
            if not existing_row[BRANCH_COL]:
                value_updates.append((f"{col_letter(BRANCH_COL)}{row_num}", [branch]))
            if is_head and time_range and not existing_row[TIME_RANGE_COL]:
                value_updates.append((f"{col_letter(TIME_RANGE_COL)}{row_num}", [time_range]))
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

    apply_value_updates(sheet_id, tab_name, access_token, value_updates)

    if to_append:
        start_row = sheets_append(sheet_id, tab_name, access_token, to_append)
        # appended rows are contiguous, so one repeatCell covers the whole block
        format_requests.append(
            format_reset_request(grid_id, start_row, PROD_COUNT_COL, num_rows=len(to_append))
        )

    apply_format_requests(sheet_id, access_token, format_requests)

    print(f"[Vault Events Sync] Filled blanks on {updated} existing row(s), "
          f"appended {appended} new row(s) to '{tab_name}'.")


def pull_sheet_to_local():
    """Mirrors Sheet1's current full state (including any manual edits to
    Training data/Accuracy rate/FP/FN made directly in the Sheet) into a
    "Vault Events" tab inside report.xlsx. This is a full overwrite of that
    tab, not a cell-by-cell merge -- the Google Sheet is the source of truth
    for this data, the local copy just mirrors it for offline reference."""
    from dotenv import load_dotenv
    import sync_sheets as ss
    from openpyxl import Workbook, load_workbook

    load_dotenv(ss.ROOT / ".env")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    tab_name = os.environ.get("VAULT_EVENTS_TAB", "Sheet1").strip()
    if not sheet_id:
        print("GOOGLE_SHEET_ID is not set in .env")
        return

    access_token = ss.get_access_token()
    sheet_rows = fetch_sheet_rows(sheet_id, tab_name, access_token)
    if not sheet_rows:
        print("[Vault Events Pull] Sheet is empty, nothing to pull.")
        return

    excel_path = ss.ROOT / "report.xlsx"
    local_tab_name = "Vault Events"
    wb = load_workbook(excel_path) if excel_path.exists() else Workbook()

    if local_tab_name in wb.sheetnames:
        wb.remove(wb[local_tab_name])
    ws = wb.create_sheet(local_tab_name)
    for row in sheet_rows:
        ws.append(row)

    if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
        wb.remove(wb["Sheet"])  # openpyxl's default blank sheet on a new workbook

    wb.save(excel_path)
    print(f"[Vault Events Pull] Mirrored {len(sheet_rows) - 1} row(s) from '{tab_name}' "
          f"into {excel_path.name}'s '{local_tab_name}' tab.")


SUMMARY_HEADER = ["Date", "Branch", "Sessions"] + EVENT_ORDER


def build_summary(sheet_rows):
    """Aggregates Sheet1's per-session rows into one row per (date, branch):
    total count per event type across all its sessions that day, plus how
    many sessions it had -- a manager-readable rollup instead of the raw
    per-session detail. Forward-fills the same blank Date/Session/Branch
    cells the raw sheet leaves blank on continuation rows."""
    totals = defaultdict(lambda: defaultdict(int))
    sessions_seen = defaultdict(set)
    last_date, last_session, last_branch = "", "", ""

    for row in sheet_rows[1:]:
        row = list(row) + [""] * (len(SHEET_COLUMNS) - len(row))
        date = row[0].strip() if row[0] else last_date
        session = row[1].strip() if row[1] else last_session
        branch = row[BRANCH_COL].strip() if row[BRANCH_COL] else last_branch
        last_date, last_session, last_branch = date, session, branch

        event_name = row[2].strip() if row[2] else ""
        if not event_name or not date or not branch:
            continue
        try:
            count = int(row[PROD_COUNT_COL])
        except (TypeError, ValueError):
            count = 0

        key = (date, branch)
        totals[key][event_name] += count
        if session:
            sessions_seen[key].add(session)

    out_rows = []
    for date, branch in sorted(totals, key=lambda k: (k[0], k[1])):
        events = totals[(date, branch)]
        row = [date, branch, len(sessions_seen[(date, branch)])]
        row += [events.get(name, 0) for name in EVENT_ORDER]
        out_rows.append(row)
    return out_rows


def ensure_tab_exists(sheet_id, access_token, tab_name):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    data = request_json(url, access_token)
    if any(s["properties"]["title"] == tab_name for s in data.get("sheets", [])):
        return
    batch_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    request_json(batch_url, access_token, method="POST",
                 body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]})


def push_summary_to_sheet():
    """Rebuilds a "Summary" tab (one row per date+branch, totals per event
    type) from Sheet1's current state. Always a full clear-and-rewrite --
    this tab is a derived view, not something anyone edits by hand."""
    from dotenv import load_dotenv
    import sync_sheets as ss

    load_dotenv(ss.ROOT / ".env")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    tab_name = os.environ.get("VAULT_EVENTS_TAB", "Sheet1").strip()
    summary_tab = os.environ.get("VAULT_SUMMARY_TAB", "Summary").strip()
    if not sheet_id:
        print("GOOGLE_SHEET_ID is not set in .env")
        return

    access_token = ss.get_access_token()
    sheet_rows = fetch_sheet_rows(sheet_id, tab_name, access_token)
    if not sheet_rows:
        print(f"[Vault Events Summary] '{tab_name}' is empty, nothing to summarize.")
        return

    summary_rows = build_summary(sheet_rows)
    ensure_tab_exists(sheet_id, access_token, summary_tab)

    clear_url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
                 f"{urllib.parse.quote(summary_tab)}!A:Z:clear")
    request_json(clear_url, access_token, method="POST", body={})

    update_url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
                  f"{urllib.parse.quote(summary_tab)}!A1?valueInputOption=USER_ENTERED")
    request_json(update_url, access_token, method="PUT",
                 body={"values": [SUMMARY_HEADER] + summary_rows})

    grid_id = get_grid_id(sheet_id, summary_tab, access_token)
    bold_header = {"requests": [{
        "repeatCell": {
            "range": {"sheetId": grid_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }
    }]}
    request_json(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate",
                 access_token, method="POST", body=bold_header)

    print(f"[Vault Events Summary] Wrote {len(summary_rows)} row(s) "
          f"(one per date+branch) to '{summary_tab}'.")


def main():
    if "--pull" in sys.argv:
        pull_sheet_to_local()
        return

    if "--summary" in sys.argv:
        push_summary_to_sheet()
        return

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    argv = sys.argv[1:]
    push = "--push" in argv

    date_filter = None
    if "--date" in argv:
        idx = argv.index("--date")
        date_filter = datetime.strptime(argv[idx + 1], "%Y-%m-%d").date()
        del argv[idx:idx + 2]

    paths = [a for a in argv if a != "--push"]

    states, detections, opened_early, spans, _ = load_events(paths)
    summary = summarize(states, detections, opened_early=opened_early)
    if date_filter:
        summary = {k: v for k, v in summary.items() if k[0] == date_filter}
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
