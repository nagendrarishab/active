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
  2. Fill in SOURCE_FOLDER and DEST_FOLDER in .env with the Drive folder
     IDs (the id in the folder's URL: drive.google.com/drive/folders/<id>).

SOURCE_FOLDER is a live per-camera upload location (raw .mp4 files, not
zips), so download_and_process_new_videos() pulls each new .mp4 straight
into ./raw and runs it through process_footage.process_video() directly --
the same motion-detect/cut/verify step process_zip() uses per extracted
video, just without an unzip in front of it. Already-processed videos are
tracked via processing_log.jsonl's "video_done" events, same resumability
model run_pipeline.py uses for zips.

upload_merged_files() pushes every file in ./active (merge_active's
30-minute output) to DEST_FOLDER, renamed to continue the active_part_NNN
numbering already present there -- merge_active always restarts its own
local numbering at active_part_000.mp4 each run, so uploading under the
local name as-is would silently overwrite whatever ended up there last
time. rclone copyto hash-verifies the transfer before returning; only on
success is the local file deleted, matching the "verify before delete"
rule the rest of the pipeline uses for raw files and clips.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from dotenv import load_dotenv

import merge_active as ma
import process_footage as pf
import run_pipeline as rp

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RCLONE_REMOTE = os.environ["RCLONE_REMOTE"]  # name of the remote created via `rclone config`
SOURCE_FOLDER = os.environ["SOURCE_FOLDER"]  # Drive folder ID the CCTV videos land in
DEST_FOLDER = os.environ["DEST_FOLDER"]      # Drive folder ID merged 30-min files go to


def _already_processed_videos():
    done = set()
    if not rp.LOG_PATH.exists():
        return done
    with rp.LOG_PATH.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("event") == "video_done":
                done.add(d["video"])
    return done


def download_and_process_new_videos():
    """Download every new video first, then run OpenCV motion-detect/cut on
    each one. A video already sitting in ./raw from a prior run that
    crashed before finishing (e.g. a missing ffmpeg) is reprocessed without
    being re-downloaded."""
    done = _already_processed_videos()
    out = subprocess.run(
        ["rclone", "lsjson", f"{RCLONE_REMOTE}:", "--drive-root-folder-id", SOURCE_FOLDER],
        capture_output=True, text=True, check=True,
    )
    entries = json.loads(out.stdout)

    pf.RAW_DIR.mkdir(parents=True, exist_ok=True)
    to_process = []
    for e in entries:
        if e.get("IsDir") or not e["Name"].endswith(".mp4"):
            continue
        local_path = pf.RAW_DIR / e["Name"]
        if str(local_path) in done:
            continue
        if not local_path.exists():
            print(f"downloading: {e['Name']}")
            subprocess.run(
                ["rclone", "copyto", f"{RCLONE_REMOTE}:{e['Name']}", str(local_path),
                 "--drive-root-folder-id", SOURCE_FOLDER],
                check=True,
            )
            pf.log_event({"event": "video_downloaded", "video": str(local_path)})
        to_process.append(local_path)

    if not to_process:
        print("no new videos on Drive")
        return

    for local_path in to_process:
        try:
            pf.log_event({"event": "video_start", "video": str(local_path)})
            pf.process_video(local_path)
        except Exception as exc:
            pf.log_event({"event": "video_error", "video": str(local_path), "error": str(exc)})

    if not found_new:
        print("no new videos on Drive")


PART_RE = re.compile(r"^active_part_(\d+)\.mp4$")


def _next_dest_index():
    out = subprocess.run(
        ["rclone", "lsjson", f"{RCLONE_REMOTE}:", "--drive-root-folder-id", DEST_FOLDER],
        capture_output=True, text=True, check=True,
    )
    entries = json.loads(out.stdout)
    indices = [int(m.group(1)) for e in entries if (m := PART_RE.match(e["Name"]))]
    return max(indices, default=-1) + 1


def upload_merged_files():
    parts = sorted(ma.MERGED_DIR.glob("*.mp4"))
    if not parts:
        print("nothing in ./active to upload")
        return

    next_index = _next_dest_index()
    for i, part in enumerate(parts):
        dest_name = f"active_part_{next_index + i:03d}.mp4"
        print(f"uploading: {part.name} -> {dest_name}")
        subprocess.run(
            ["rclone", "copyto", str(part), f"{RCLONE_REMOTE}:{dest_name}",
             "--drive-root-folder-id", DEST_FOLDER],
            check=True,
        )
        pf.log_event({"event": "upload_verified", "file": str(part), "dest_name": dest_name})
        part.unlink()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "upload":
        upload_merged_files()
    else:
        download_and_process_new_videos()
