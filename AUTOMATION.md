# End-to-end flow of `./automate.py`

**1. Check Drive for new footage** (`drive_sync.download_and_process_new_videos()`)
- `rclone lsjson gdrive: --drive-root-folder-id <SOURCE_FOLDER>` lists everything in the source folder.
- For each `.mp4` there, skip it if it's already downloaded locally (`./raw/<name>`) or already logged as `video_done` in the past.
- For each new one: `rclone copyto` pulls it into `./raw/`, logs `video_downloaded`, then immediately calls `process_footage.process_video()` on it (see step 3) — one video at a time, so disk usage never balloons.

**2. Process any manually-dropped zips** (leftover capability, in case a zip is handed over again)
- Globs `Camera Footage-*.zip` in the project folder, skips any already marked `zip_verified`, otherwise extracts + processes it (`process_footage.process_zip`) the same way step 3 processes a raw video, then deletes the zip.

**3. Per-video processing** (`process_footage.process_video()`, called from both steps above)
- `ffprobe` gets the duration (or attempts an error-tolerant remux recovery if the file's unreadable).
- OpenCV walks the video, sampling a frame every 0.5s, diffing against the previous sample to build a motion/no-motion timeline.
- That timeline is turned into padded, merged `(start, end, is_motion)` segments.
- Each segment is cut with `ffmpeg` (stream copy) into `./active_tmp` (motion) or `./idle` (no motion), and immediately decode-verified (`ffmpeg -f null`) — retried once from the raw file if the first cut fails, discarded only after a second failure.
- The original raw file is deleted once every segment from it is settled.

**4. Merge** (`run_pipeline.merge_and_verify()`)
- `merge_active.main()` sorts everything currently in `./active_tmp` chronologically (by filename timestamp) and concatenates + splits it into 30-minute `active_part_NNN.mp4` files in `./active`.
- Those merged files are decode-verified.
- Only if all of them pass does it delete `./active_tmp`.

**5. Upload** (`drive_sync.upload_merged_files()`)
- Checks what's already in `DEST_FOLDER` on Drive to find the highest existing `active_part_NNN` index.
- Uploads each local merged file under the next available index (`rclone copyto`, hash-verified).
- Deletes the local copy only after a successful, verified upload.

## Does video detail get logged to `processing_log.jsonl`?

Yes — every stage appends a JSON line. For a single downloaded video you'd see, in order:

```
{"event": "video_downloaded", "video": ".../raw/Camera_....mp4", "ts": ...}
{"event": "video_start", "video": ".../raw/Camera_....mp4", "ts": ...}
{"event": "cut_retry", "clip": "...", "attempt": 1, "error": "...", "ts": ...}   # only if a cut needed a retry
{"event": "video_done", "video": ".../raw/Camera_....mp4",
 "duration_sec": 300.0, "active_segments": 2, "idle_segments": 3,
 "active_time_sec": 45.2, "idle_time_sec": 254.8,
 "corrupted_segments": 0, "recovered": false, "ts": ...}
```

`video_done` is the one that carries the actual per-video stats (duration, how much was active vs. idle, segment counts) — it's also what `_already_processed_videos()` in `drive_sync.py` checks to decide whether a video's already been handled, so it doubles as both the audit record and the resume marker. If a video is unrecoverably corrupt you'd instead see `video_corrupted` (and the raw file gets deleted with no `video_done`). Zip-based runs additionally log `zip_verified`/`zip_deleted`, and uploads log `upload_verified` with the local file path and the Drive name it was uploaded as.
