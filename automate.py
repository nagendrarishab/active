#!/opt/anaconda3/bin/python3
"""One-shot automation: pull new footage zips from Drive, run the existing
extract/detect/merge pipeline, then push the merged 30-minute files to Drive.

Requires drive_sync.py's one-time rclone setup to be done first (see its
docstring). Safe to run repeatedly / on a schedule -- every step it calls
already resumes from processing_log.jsonl or from what's still on disk, so
a re-run just picks up where the last one left off.

Usage:
    ./automate.py
"""
import subprocess
import sys
from pathlib import Path

import drive_sync as ds

ROOT = Path(__file__).resolve().parent


def main():
    print("=== checking Drive for new footage zips ===")
    ds.download_new_zips()

    print("=== running extract / motion-detect / merge pipeline ===")
    subprocess.run(
        [sys.executable, str(ROOT / "run_pipeline.py"), "--all", "--delete-zips"],
        check=True,
    )

    print("=== uploading merged output to Drive ===")
    ds.upload_merged_files()


if __name__ == "__main__":
    main()
