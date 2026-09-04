#!/opt/anaconda3/bin/python3
"""Google Drive sync for the footage pipeline, via rclone.

One-time setup before this will work:
  1. Create the rclone remote named RCLONE_REMOTE (see .env) below:
         rclone config create gdrive drive scope=drive
     This opens a browser for you to sign in and writes the real config
     (with your OAuth token) to ~/.config/rclone/rclone.conf -- see
     rclone.conf.example in this folder for the shape of that entry.
     A token can't be hand-written into a config file, it has to come
     from that interactive step.
  2. Fill in SOURCE_FOLDER and DEST_FOLDER in .env with the actual Drive
     paths (relative to your Drive root, e.g. "CCTV/raw footage").

download_new_zips() lists SOURCE_FOLDER and pulls any .zip not already
marked zip_verified in processing_log.jsonl and not already sitting
here locally -- reuses run_pipeline's own resume bookkeeping, so it
never re-downloads something already processed.

upload_merged_files() pushes every file in ./active (merge_active's
30-minute output) to DEST_FOLDER. rclone copyto hash-verifies the
transfer before returning; only on success is the local file deleted,
matching the "verify before delete" rule the rest of the pipeline uses
for raw files and clips.
"""
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

import merge_active as ma
import run_pipeline as rp
from process_footage import log_event

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RCLONE_REMOTE = os.environ["RCLONE_REMOTE"]  # name of the remote created via `rclone config`
SOURCE_FOLDER = os.environ["SOURCE_FOLDER"]  # Drive folder the CCTV zips land in
DEST_FOLDER = os.environ["DEST_FOLDER"]      # Drive folder merged 30-min files go to


def _source():
    return f"{RCLONE_REMOTE}:{SOURCE_FOLDER}"


def _dest():
    return f"{RCLONE_REMOTE}:{DEST_FOLDER}"


def download_new_zips():
    done = rp.already_verified_zips()
    out = subprocess.run(
        ["rclone", "lsjson", _source()],
        capture_output=True, text=True, check=True,
    )
    entries = json.loads(out.stdout)

    new_names = []
    for e in entries:
        if e.get("IsDir") or not e["Name"].endswith(".zip"):
            continue
        local_path = ROOT / e["Name"]
        if str(local_path) in done or local_path.exists():
            continue
        new_names.append(e["Name"])

    if not new_names:
        print("no new zips on Drive")
        return

    for name in new_names:
        print(f"downloading: {name}")
        subprocess.run(
            ["rclone", "copyto", f"{_source()}/{name}", str(ROOT / name)],
            check=True,
        )
        log_event({"event": "zip_downloaded", "zip": str(ROOT / name)})


def upload_merged_files():
    parts = sorted(ma.MERGED_DIR.glob("*.mp4"))
    if not parts:
        print("nothing in ./active to upload")
        return

    for part in parts:
        print(f"uploading: {part.name}")
        subprocess.run(
            ["rclone", "copyto", str(part), f"{_dest()}/{part.name}"],
            check=True,
        )
        log_event({"event": "upload_verified", "file": str(part)})
        part.unlink()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "upload":
        upload_merged_files()
    else:
        download_new_zips()
