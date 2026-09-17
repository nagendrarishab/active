#!/opt/anaconda3/bin/python3
"""Check whether each configured branch (drive_sync.SOURCES) is still
uploading footage to its Drive source folder.

Each camera drops a new .mp4 into its source folder every 15 minutes, so a
branch counts as "stopped" once its newest file is older than STALE_AFTER_MIN
minutes -- or if the folder has no files at all.

Usage:
    python check_sources.py            # prints a status line per branch,
                                        # exits 1 if any branch is stale
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import drive_sync as ds

STALE_AFTER_MIN = 45  # 3x the 15-min upload interval, to allow for jitter


def latest_mtime(source_folder: str):
    out = subprocess.run(
        ["rclone", "lsjson", f"{ds.RCLONE_REMOTE}:", "--drive-root-folder-id", source_folder],
        capture_output=True, text=True, check=True,
    )
    entries = ds.json.loads(out.stdout)
    if not entries:
        return None
    latest = max(e["ModTime"] for e in entries)
    return datetime.strptime(latest, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def main():
    now = datetime.now(timezone.utc)
    any_stale = False

    for source_folder, source in ds.SOURCES:
        mtime = latest_mtime(source_folder)
        if mtime is None:
            print(f"{source}: STALE - source folder is empty")
            any_stale = True
            continue

        age = now - mtime
        status = "STALE" if age > timedelta(minutes=STALE_AFTER_MIN) else "ok"
        print(f"{source}: {status} - latest file {mtime.isoformat()} ({age} ago)")
        if status == "STALE":
            any_stale = True

    sys.exit(1 if any_stale else 0)


if __name__ == "__main__":
    main()
