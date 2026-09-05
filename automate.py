#!/opt/anaconda3/bin/python3
"""One-shot automation: pull new footage from Drive (raw .mp4s, plus any
"Camera Footage-*.zip" you've dropped in this folder yourself), run it
through the existing motion-detect/cut/merge pipeline, then push the merged
30-minute files back to Drive.

Requires drive_sync.py's one-time rclone setup to be done first (see its
docstring). Safe to run repeatedly / on a schedule -- every step it calls
already resumes from processing_log.jsonl or from what's still on disk, so
a re-run just picks up where the last one left off.

Usage:
    ./automate.py
"""
import drive_sync as ds
import export_report as er
import run_pipeline as rp


def main():
    print("=== checking Drive for new footage ===")
    ds.download_and_process_new_videos()

    print("=== processing any zip files dropped in this folder ===")
    done = rp.already_verified_zips()
    zips = sorted(rp.ROOT.glob("Camera Footage-*.zip"))
    for zip_path in zips:
        if str(zip_path) in done:
            continue
        print(f"processing: {zip_path.name}")
        rp.process_one_zip(zip_path, delete_zip=True)

    print("=== merging active_tmp -> active ===")
    rp.merge_and_verify()

    print("=== uploading merged output to Drive ===")
    ds.upload_merged_files()

    print("=== syncing report.xlsx ===")
    er.main()


if __name__ == "__main__":
    main()
