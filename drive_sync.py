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

upload_merged_files() pushes every not-yet-uploaded file in ./active
(merge_active's 30-minute output, which is no longer deleted after upload --
kept as a local copy) to DEST_FOLDER, renamed to continue the
active_part_NNN numbering already present there. rclone copyto hash-verifies
the transfer before returning; on success it's logged as "upload_verified",
which is what determines "not-yet-uploaded" on the next run -- since the
local file sticks around, presence-on-disk can't be used for that the way
it is for raw videos.
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
    """Download and process one video at a time -- download, then
    immediately run it through process_video() (which deletes the raw file
    once it's settled) before moving on to the next -- so only one raw video
    occupies disk space at a time, and the combining (merge) step only
    starts once every video has been downloaded and split into
    active_tmp/idle. A video already sitting in ./raw from a prior run that
    crashed before finishing (e.g. a missing ffmpeg) is reprocessed without
    being re-downloaded."""
    done = _already_processed_videos()
    out = subprocess.run(
        ["rclone", "lsjson", f"{RCLONE_REMOTE}:", "--drive-root-folder-id", SOURCE_FOLDER],
        capture_output=True, text=True, check=True,
    )
    entries = json.loads(out.stdout)

    pf.RAW_DIR.mkdir(parents=True, exist_ok=True)
    found_new = False
    for e in entries:
        if e.get("IsDir") or not e["Name"].endswith(".mp4"):
            continue
        local_path = pf.RAW_DIR / e["Name"]
        if str(local_path) in done:
            continue
        found_new = True

        if not local_path.exists():
            print(f"downloading: {e['Name']}")
            subprocess.run(
                ["rclone", "copyto", f"{RCLONE_REMOTE}:{e['Name']}", str(local_path),
                 "--drive-root-folder-id", SOURCE_FOLDER],
                check=True,
            )
            pf.log_event({"event": "video_downloaded", "video": str(local_path)})

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


def _already_uploaded_files():
    done = set()
    if not rp.LOG_PATH.exists():
        return done
    with rp.LOG_PATH.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("event") == "upload_verified":
                done.add(d["file"])
    return done


def upload_merged_files():
    done = _already_uploaded_files()
    parts = [p for p in sorted(ma.MERGED_DIR.glob("*.mp4")) if str(p) not in done]
    if not parts:
        print("nothing new in ./active to upload")
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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "upload":
        upload_merged_files()
    else:
        download_and_process_new_videos()
