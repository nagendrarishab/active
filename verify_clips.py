#!/opt/anaconda3/bin/python3
"""Decode-check every .mp4 in one or more folders to find corrupted clips.

ffprobe alone isn't enough -- some corrupted files still report a duration
fine but fail partway through an actual decode. This does a real decode pass
(ffmpeg -f null) on every file, in parallel, and lists anything that errors.

Usage:
    python3 verify_clips.py                # checks ./active by default
    python3 verify_clips.py idle active_tmp
    python3 verify_clips.py active/some_file.mp4
"""
import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

MAX_WORKERS = 8


def check_file(path: Path) -> Optional[str]:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return result.stderr.strip() or None


def collect_files(targets):
    files = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            files.extend(sorted(p.glob("*.mp4")))
        elif p.is_file():
            files.append(p)
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", default=["active"],
                         help="Folders and/or .mp4 files to check (default: active)")
    args = parser.parse_args()

    files = collect_files(args.targets)
    if not files:
        print("No .mp4 files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Checking {len(files)} file(s)...")
    bad = []
    checked = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_file, f): f for f in files}
        for fut in as_completed(futures):
            f = futures[fut]
            err = fut.result()
            checked += 1
            if err:
                bad.append((f, err))
                print(f"BAD: {f}")
            if checked % 200 == 0:
                print(f"  checked {checked}/{len(files)}...")

    print(f"\n{len(bad)} bad file(s) out of {len(files)} checked")
    for f, err in bad:
        print(f"  {f}: {err.splitlines()[0]}")

    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
