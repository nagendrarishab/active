#!/opt/anaconda3/bin/python3
"""End-to-end CCTV pipeline: unzip -> motion-detect/cut -> verify -> merge -> cleanup.

Processes one zip at a time to keep disk usage bounded:
  1. Extract the zip, run motion detection on each video, cut into
     ./active_tmp and ./idle (process_footage.process_zip). Each cut clip is
     decode-verified immediately, with one retry from the still-present raw
     file if it fails -- ffprobe alone can miss mid-stream corruption, and
     not every cut failure means the source is actually corrupt (an
     interrupted ffmpeg or a disk hiccup isn't). Only once every clip from a
     video is settled (good, or confirmed corrupt even after retry) does the
     raw extracted file get deleted -- see process_footage.cut_and_verify.
  2. Mark the zip complete in the log so a re-run skips it. Optionally
     delete the source zip (--delete-zips) now that its data is safely
     extracted and verified elsewhere.
Once every requested zip is done, merge ./active_tmp into fixed-length files
in ./active (merge_active.main -- this needs the full chronological set, so
it only makes sense to run once at the end, not per zip). The merged output
itself is decode-verified too (concatenation is a separate step that could
in principle introduce its own issues) before ./active_tmp is deleted.

Resumable: zips already marked "zip_verified" in processing_log.jsonl are
skipped on a re-run, so restarting after an interruption -- or after editing
this pipeline's code -- doesn't reprocess (and duplicate) work already done.
If you deliberately want to redo an already-verified zip, remove its clips
from ./active_tmp and ./idle first, since this script won't overwrite them.

Usage:
    ./run_pipeline.py --all                  # process every zip, keep them
    ./run_pipeline.py --all --delete-zips    # process every zip, delete each as it's verified
    ./run_pipeline.py z1.zip z2.zip          # process specific zips
    ./run_pipeline.py --all --skip-merge     # process but don't merge yet
"""
import argparse
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import process_footage as pf
import merge_active as ma
from verify_clips import check_file

ROOT = pf.ROOT
LOG_PATH = pf.LOG_PATH


def already_verified_zips():
    done = set()
    if not LOG_PATH.exists():
        return done
    with LOG_PATH.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("event") == "zip_verified":
                done.add(d["zip"])
    return done


def verify_new_clips(paths):
    bad = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for path, err in zip(paths, pool.map(check_file, paths)):
            if err:
                bad.append(path)
    return bad


def process_one_zip(zip_path: Path, delete_zip: bool):
    # process_zip's returned clips are already decode-verified (with retry)
    # per-clip inside process_video -- see process_footage.cut_and_verify.
    created = pf.process_zip(zip_path)

    pf.log_event({"event": "zip_verified", "zip": str(zip_path), "clips": len(created)})
    print(f"  {zip_path.name}: {len(created)} verified clips")

    if delete_zip:
        zip_path.unlink()
        pf.log_event({"event": "zip_deleted", "zip": str(zip_path)})


def merge_and_verify():
    """Merge ./active_tmp into fixed-length files in ./active, decode-verify
    the merged output, and only then delete ./active_tmp. Shared by main()
    and by automate.py, which drives this same tail end after downloading
    and processing raw videos directly instead of zips."""
    print("merging active_tmp -> active ...")
    new_parts = ma.main()

    if not ma.ACTIVE_DIR.exists():
        # Nothing was ever cut into active_tmp this run (e.g. every source
        # video failed before producing a clip) -- nothing to merge or clean up.
        return

    # Only decode-verify what this run actually produced -- older parts from
    # a previous run are still sitting in ./active (no longer deleted after
    # upload) and were already verified when they were created.
    bad = verify_new_clips(new_parts)
    if bad:
        print(f"WARNING: {len(bad)} merged file(s) failed decode-verify, NOT deleting active_tmp:")
        for b in bad:
            print(f"  BAD: {b}")
        return

    print(f"merged output verified clean ({len(new_parts)} files), removing {ma.ACTIVE_DIR}")
    shutil.rmtree(ma.ACTIVE_DIR)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("zips", nargs="*", help="Zip files to process")
    parser.add_argument("--all", action="store_true", help="Process every 'Camera Footage-*.zip' in cwd")
    parser.add_argument("--delete-zips", action="store_true",
                         help="Delete each source zip right after its clips are extracted and verified")
    parser.add_argument("--skip-merge", action="store_true",
                         help="Don't run the final merge step (e.g. if you're processing zips in batches)")
    args = parser.parse_args()

    zips = sorted(ROOT.glob("Camera Footage-*.zip")) if args.all else [Path(z) for z in args.zips]
    if not zips:
        print("No zip files specified.", file=sys.stderr)
        sys.exit(1)

    done = already_verified_zips()
    for zip_path in zips:
        if str(zip_path) in done:
            print(f"skip (already verified): {zip_path.name}")
            continue
        print(f"processing: {zip_path.name}")
        process_one_zip(zip_path, args.delete_zips)

    if args.skip_merge:
        print("skipping merge (--skip-merge)")
        return

    print("all zips done, ", end="")
    merge_and_verify()


if __name__ == "__main__":
    main()
