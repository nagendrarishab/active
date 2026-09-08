#!/opt/anaconda3/bin/python3
"""Sync footage report data from report.xlsx / processing_log.jsonl directly to Google Sheets.

Uses the Google Sheets API with the authenticated Google OAuth token from rclone.conf.

Usage:
    # Sync all missing rows from report.xlsx to Google Sheet:
    python sync_sheets.py --all

    # Test connection and view current sheet status:
    python sync_sheets.py --test
"""
import argparse
import configparser
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time
from pathlib import Path

from dotenv import load_dotenv
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RCLONE_CONF_PATH = Path("~/.config/rclone/rclone.conf").expanduser()
EXCEL_PATH = ROOT / "report.xlsx"
DEFAULT_SHEET_TAB = "Videos"


def get_access_token():
    """Fetches a fresh Google OAuth access token using credentials from rclone.conf."""
    if not RCLONE_CONF_PATH.exists():
        raise FileNotFoundError(f"rclone config file not found at {RCLONE_CONF_PATH}")

    cfg = configparser.ConfigParser()
    cfg.read(RCLONE_CONF_PATH)

    remote = os.environ.get("RCLONE_REMOTE", "gdrive")
    if not cfg.has_section(remote):
        raise ValueError(f"Section [{remote}] not found in {RCLONE_CONF_PATH}")

    client_id = cfg.get(remote, "client_id")
    client_secret = cfg.get(remote, "client_secret")
    token_data = json.loads(cfg.get(remote, "token"))
    refresh_token = token_data.get("refresh_token")

    if not refresh_token:
        raise ValueError(f"No refresh_token found in [{remote}] section of {RCLONE_CONF_PATH}")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        return res["access_token"]


def serialize_cell(val):
    if isinstance(val, (date, datetime, time)):
        return val.isoformat()
    if val is None:
        return ""
    return val


def serialize_row(row):
    return [serialize_cell(c) for c in row]


def get_existing_videos_from_sheet(sheet_id, tab_name, access_token):
    """Reads column C (video names) from the Google Sheet to avoid duplicate rows."""
    range_name = urllib.parse.quote(f"{tab_name}!C:C")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            values = data.get("values", [])
            existing = set()
            for r in values[1:]:  # skip header row
                if r and r[0]:
                    existing.add(str(r[0]).strip())
            return existing
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return set()
        raise


def sync_rows_via_api(rows, sheet_id, tab_name):
    """Appends rows to Google Sheet via Google Sheets API, deduplicating by Video name."""
    if not rows:
        return True, "no rows to sync"

    access_token = get_access_token()
    existing_videos = get_existing_videos_from_sheet(sheet_id, tab_name, access_token)

    to_append = []
    for r in rows:
        serialized = serialize_row(r)
        # Column 2 (index 2) is video name: Date(0), Time(1), Video(2)
        video_name = str(serialized[2] if len(serialized) > 2 else "").strip()
        if not video_name or video_name not in existing_videos:
            to_append.append(serialized)
            if video_name:
                existing_videos.add(video_name)

    if not to_append:
        print("[Google Sheets Sync] All rows are already present in Google Sheet.")
        return True, "already up to date"

    range_name = urllib.parse.quote(f"{tab_name}!A:I")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}:append?valueInputOption=USER_ENTERED"

    body = json.dumps({"values": to_append}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        updated_rows = res.get("updates", {}).get("updatedRows", len(to_append))
        print(f"[Google Sheets Sync] Successfully appended {updated_rows} row(s) to Google Sheet.")
        return True, f"added {updated_rows} row(s)"


def send_to_webhook(rows):
    """Main sync entrypoint. Uses Direct Google Sheets API (Path A) when GOOGLE_SHEET_ID is set."""
    load_dotenv(ROOT / ".env")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    tab_name = os.environ.get("GOOGLE_SHEET_TAB", DEFAULT_SHEET_TAB).strip()

    if sheet_id:
        try:
            return sync_rows_via_api(rows, sheet_id, tab_name)
        except Exception as exc:
            print(f"[Google Sheets Sync] Error syncing to Sheets API: {exc}")
            return False, str(exc)

    print("[Google Sheets Sync] GOOGLE_SHEET_ID not configured in .env -- skipping Google Sheet update.")
    return False, "GOOGLE_SHEET_ID not configured"


def sync_all_from_excel():
    """Reads all rows from report.xlsx and synchronizes them to Google Sheet."""
    if not EXCEL_PATH.exists():
        print(f"Error: {EXCEL_PATH} does not exist.")
        return

    wb = load_workbook(EXCEL_PATH, data_only=True)
    tab_name = os.environ.get("GOOGLE_SHEET_TAB", DEFAULT_SHEET_TAB).strip()
    ws = wb[tab_name] if tab_name in wb.sheetnames else wb.active

    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if any(row):
            rows.append(list(row))

    if not rows:
        print("No data rows found in report.xlsx.")
        return

    print(f"Checking {len(rows)} row(s) from {EXCEL_PATH.name} against Google Sheet...")
    send_to_webhook(rows)


def test_connection():
    load_dotenv(ROOT / ".env")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    tab_name = os.environ.get("GOOGLE_SHEET_TAB", DEFAULT_SHEET_TAB).strip()

    if not sheet_id:
        print("GOOGLE_SHEET_ID is not set in .env")
        return

    print(f"Testing connection to Google Sheet ID: {sheet_id}")
    try:
        access_token = get_access_token()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            title = data.get("properties", {}).get("title", "")
            sheets = [s["properties"]["title"] for s in data.get("sheets", [])]
            print(f"SUCCESS! Connected to spreadsheet: '{title}'")
            print(f"Available worksheets: {sheets}")

        existing = get_existing_videos_from_sheet(sheet_id, tab_name, access_token)
        print(f"Worksheet '{tab_name}' currently contains {len(existing)} unique video rows.")
    except Exception as exc:
        print(f"Connection test failed: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync report to Google Sheet")
    parser.add_argument("--all", action="store_true", help="Sync all rows from report.xlsx to Google Sheet")
    parser.add_argument("--test", action="store_true", help="Test Google Sheet API connection")
    args = parser.parse_args()

    if args.all:
        sync_all_from_excel()
    elif args.test:
        test_connection()
    else:
        parser.print_help()
