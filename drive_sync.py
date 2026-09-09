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
     SOURCE_FOLDER accepts a comma-separated list to watch more than one
     source folder, e.g. SOURCE_FOLDER=id1,id2.
  3. Optionally set SOURCE_NAMES to a comma-separated list of human-readable
     labels, positionally matched to SOURCE_FOLDER, e.g.
     SOURCE_NAMES=lobby,warehouse. If omitted (or shorter than
     SOURCE_FOLDER), missing labels default to source1, source2, etc.

SOURCE_FOLDER is one or more live per-camera upload locations (raw .mp4
files, not zips), so download_and_process_new_videos() pulls each new .mp4
from every configured folder straight into ./raw and runs it through
process_footage.process_video() directly -- the same motion-detect/cut/
verify step process_zip() uses per extracted video, just without an unzip
in front of it. Already-processed videos are tracked via
processing_log.jsonl's "video_done" events, same resumability model
run_pipeline.py uses for zips.

Each source folder's clips are kept in their own ./active_tmp/<name> and
./idle/<name> subtree (SOURCES below maps each folder ID to its label), so
merge_active.py never interleaves footage from different source folders
into the same merged video -- each source gets its own active_part_NNN
series in ./active/<name>.

upload_merged_files() pushes every not-yet-uploaded file in ./active/<name>
(merge_active's 30-minute output) to DEST_FOLDER/<name> on Drive, renamed to
continue that source's active_part_NNN numbering. rclone copyto hash-verifies
the transfer before returning; on success it is logged as "upload_verified"
and the local file is deleted immediately to free up disk space.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv

import merge_active as ma
import process_footage as pf
import run_pipeline as rp

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RCLONE_REMOTE = os.environ["RCLONE_REMOTE"]  # name of the remote created via `rclone config`
SOURCE_FOLDERS = [f.strip() for f in os.environ["SOURCE_FOLDER"].split(",") if f.strip()]  # Drive folder ID(s) CCTV videos land in
DEST_FOLDER = os.environ["DEST_FOLDER"]      # Drive folder ID merged 30-min files go to

_SOURCE_NAMES = [n.strip() for n in os.environ.get("SOURCE_NAMES", "").split(",") if n.strip()]
# Each source folder's label -- used to keep its footage in its own
# ./active_tmp/<label>, ./idle/<label>, ./active/<label>, and
# DEST_FOLDER/<label> on Drive, so different source folders' footage is
# never merged together.
SOURCES = [
    (folder_id, _SOURCE_NAMES[i] if i < len(_SOURCE_NAMES) else f"source{i + 1}")
    for i, folder_id in enumerate(SOURCE_FOLDERS)
]


def _already_processed_videos():
    done = set()
    if not rp.LOG_PATH.exists():
        return done
    with rp.LOG_PATH.open() as f:
        for line in f:
            for chunk in line.replace("}{", "}\n{").splitlines():
                try:
                    d = json.loads(chunk)
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
    starts once every video from every configured source folder has been
    downloaded and split into active_tmp/idle. A video already sitting in
    ./raw from a prior run that crashed before finishing (e.g. a missing
    ffmpeg) is reprocessed without being re-downloaded."""
    done = _already_processed_videos()
    pf.RAW_DIR.mkdir(parents=True, exist_ok=True)
    found_new = False

    for source_folder, source in SOURCES:
        out = subprocess.run(
            ["rclone", "lsjson", f"{RCLONE_REMOTE}:", "--drive-root-folder-id", source_folder],
            capture_output=True, text=True, check=True,
        )
        entries = json.loads(out.stdout)

        for e in entries:
            if e.get("IsDir") or not e["Name"].endswith(".mp4"):
                continue
            local_path = pf.RAW_DIR / e["Name"]
            if str(local_path) in done:
                continue
            found_new = True

            if not local_path.exists():
                print(f"downloading: {e['Name']} (from {source})")
                subprocess.run(
                    ["rclone", "copyto", f"{RCLONE_REMOTE}:{e['Name']}", str(local_path),
                     "--drive-root-folder-id", source_folder],
                    check=True,
                )
                pf.log_event({"event": "video_downloaded", "video": str(local_path), "source": source})

            try:
                pf.log_event({"event": "video_start", "video": str(local_path), "source": source})
                pf.process_video(local_path, source=source)
            except Exception as exc:
                pf.log_event({"event": "video_error", "video": str(local_path), "source": source, "error": str(exc)})

    if not found_new:
        print("no new videos on Drive")


PART_RE = re.compile(r"^active_part_(\d+)\.mp4$")


def _next_dest_index(source: str):
    """Query Drive to find the highest active_part_NNN.mp4 index already in
    DEST_FOLDER/<source> (that per-source subfolder may not exist yet)."""
    try:
        out = subprocess.run(
            ["rclone", "lsjson", f"{RCLONE_REMOTE}:{source}", "--drive-root-folder-id", DEST_FOLDER],
            capture_output=True, text=True, check=True,
        )
        entries = json.loads(out.stdout)
        indices = [int(m.group(1)) for e in entries if (m := PART_RE.match(e["Name"]))]
    except Exception as exc:
        print(f"Notice: Drive folder check for '{source}' encountered: {exc}")
        indices = []
    return max(indices, default=-1) + 1


def upload_merged_files():
    """Uploads each source's merged files (./active/<source>) into its own
    DEST_FOLDER/<source> subfolder on Drive -- rclone creates that subfolder
    on first upload -- keeping every source's active_part_NNN numbering
    independent so footage from different source folders is never combined."""
    # Files still present in ./active/<source> are the ones not yet uploaded --
    # anything already uploaded was deleted (part.unlink()) right after.
    source_dirs = sorted(p for p in ma.MERGED_DIR.iterdir() if p.is_dir()) if ma.MERGED_DIR.exists() else []
    if not source_dirs:
        print("nothing new in ./active to upload")
        return

    for source_dir in source_dirs:
        source = source_dir.name
        parts = sorted(source_dir.glob("*.mp4"))
        if not parts:
            continue
        next_index = _next_dest_index(source)
        for i, part in enumerate(parts):
            dest_name = f"active_part_{next_index + i:03d}.mp4"
            print(f"uploading: {source}/{part.name} -> {source}/{dest_name}")
            subprocess.run(
                ["rclone", "copyto", str(part), f"{RCLONE_REMOTE}:{source}/{dest_name}",
                 "--drive-root-folder-id", DEST_FOLDER],
                check=True,
            )
            part.unlink()
            print(f"deleted local: {source}/{part.name}")

    # All uploads verified successfully -- clean up intermediate clip directories.
    # active_tmp (motion clips that were merged, grouped by source subdir): may
    # already be gone from merge_and_verify(), but clean up if still present
    # (e.g. merge verify failed earlier but uploads still ran).
    if ma.ACTIVE_DIR.exists() and any(ma.ACTIVE_DIR.iterdir()):
        shutil.rmtree(ma.ACTIVE_DIR)
        ma.ACTIVE_DIR.mkdir()   # re-create empty so downstream globs never error
        print(f"cleaned: {ma.ACTIVE_DIR.name}/")

    # idle (non-motion clips, grouped by source subdir), no longer needed once
    # the active footage is safely on Drive.
    idle_clips = list(pf.IDLE_DIR.glob("*/*.mp4")) if pf.IDLE_DIR.exists() else []
    if idle_clips:
        for clip in idle_clips:
            clip.unlink()
        for d in sorted(pf.IDLE_DIR.iterdir(), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        print(f"cleaned: {pf.IDLE_DIR.name}/ ({len(idle_clips)} clips across sources)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "upload":
        upload_merged_files()
    else:
        download_and_process_new_videos()
