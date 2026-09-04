#!/opt/anaconda3/bin/python3
"""Extract CCTV zip archives and split each video into active (motion) and idle
(no motion) segments.

Usage:
    python3 process_footage.py <zip1> [zip2 ...]
    python3 process_footage.py --all          # process every "Camera Footage-*.zip" in cwd

Processes one video at a time: extract -> detect motion -> cut segments with
ffmpeg (stream copy, no re-encode) -> delete the raw extracted file. Keeps
disk usage bounded since ./raw is only a transient staging area.
"""
import argparse
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import cv2

from verify_clips import check_file as decode_check

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw"
ACTIVE_DIR = ROOT / "active_tmp"
IDLE_DIR = ROOT / "idle"
LOG_PATH = ROOT / "processing_log.jsonl"

# Motion detection tuning
SAMPLE_INTERVAL_SEC = 0.5      # analyze one frame every N seconds
DIFF_PIXEL_THRESHOLD = 25      # per-pixel grayscale diff to count as "changed"
MOTION_AREA_FRACTION = 0.015   # fraction of pixels changed to call a sample "motion"
MERGE_GAP_SEC = 2.0            # merge motion segments separated by less than this
PAD_SEC = 1.0                  # pad each motion segment by this much on each side
MIN_SEGMENT_SEC = 1.0          # drop segments shorter than this (noise)
ANALYSIS_WIDTH = 480           # downscale frames to this width before diffing (cuts are still full-res)

USE_OPENCL = cv2.ocl.haveOpenCL()
if USE_OPENCL:
    cv2.ocl.setUseOpenCL(True)


def log_event(event: dict):
    event["ts"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")
    print(json.dumps(event))


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def try_recover(path: Path):
    """Some source files have corrupted H.264 headers (DVR write glitches) that
    make ffprobe/OpenCV refuse them outright. Attempt an error-tolerant remux;
    return a path to the recovered copy if it contains any actual video, else
    None (the file is unrecoverable)."""
    recovered = path.with_name(path.stem + ".recovered.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-err_detect", "ignore_err",
         "-fflags", "+genpts+discardcorrupt", "-i", str(path),
         "-c", "copy", str(recovered)],
        capture_output=True,
    )
    try:
        duration = ffprobe_duration(recovered)
    except (subprocess.CalledProcessError, ValueError):
        duration = 0.0
    if duration > 0:
        return recovered
    recovered.unlink(missing_ok=True)
    return None


def detect_motion_samples(path: Path):
    """Returns list of (timestamp_sec, is_motion), sampled roughly every
    SAMPLE_INTERVAL_SEC by walking frames sequentially (cap.grab() to skip,
    cap.retrieve() only on sampled frames) rather than seeking per-sample --
    seeking repeatedly via CAP_PROP_POS_MSEC is slower and less reliable
    across codecs."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open {path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_interval = max(1, round(fps * SAMPLE_INTERVAL_SEC))

    samples = []
    prev_gray = None
    frame_idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if frame_idx % frame_interval == 0:
            ok, frame = cap.retrieve()
            if ok:
                t = frame_idx / fps
                img = cv2.UMat(frame) if USE_OPENCL else frame
                h, w = frame.shape[:2]
                if w > ANALYSIS_WIDTH:
                    img = cv2.resize(img, (ANALYSIS_WIDTH, round(h * ANALYSIS_WIDTH / w)))
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (21, 21), 0)

                if prev_gray is None:
                    samples.append((t, False))
                else:
                    diff = cv2.absdiff(prev_gray, gray)
                    diff_np = diff.get() if USE_OPENCL else diff
                    changed = (diff_np > DIFF_PIXEL_THRESHOLD).sum()
                    frac = changed / diff_np.size
                    samples.append((t, frac >= MOTION_AREA_FRACTION))

                prev_gray = gray
        frame_idx += 1

    cap.release()
    return samples


def samples_to_segments(samples, duration: float):
    """Turn a motion/no-motion sample series into merged, padded (start, end, is_motion)
    segments covering [0, duration]."""
    if not samples:
        return [(0.0, duration, False)]

    # Collapse consecutive same-label samples into raw segments.
    raw = []
    seg_start, seg_label = samples[0][0], samples[0][1]
    for i in range(1, len(samples)):
        t, label = samples[i]
        if label != seg_label:
            raw.append((seg_start, t, seg_label))
            seg_start, seg_label = t, label
    raw.append((seg_start, duration, seg_label))

    # Merge motion segments across small non-motion gaps.
    merged = []
    for seg in raw:
        if (merged and seg[2] and merged[-1][2]
                and seg[0] - merged[-1][1] <= MERGE_GAP_SEC):
            merged[-1] = (merged[-1][0], seg[1], True)
        elif (merged and not seg[2] and merged[-1][2]
                and seg[1] - seg[0] <= MERGE_GAP_SEC):
            # Tiny idle gap sandwiched right after a motion run: fold into motion.
            merged[-1] = (merged[-1][0], seg[1], True)
        else:
            merged.append(seg)

    # Re-collapse any adjacent same-label segments created by the merge step above.
    collapsed = [merged[0]]
    for seg in merged[1:]:
        if seg[2] == collapsed[-1][2]:
            collapsed[-1] = (collapsed[-1][0], seg[1], seg[2])
        else:
            collapsed.append(seg)

    # Pad motion segments, clipped to [0, duration], then rebuild the idle gaps between them.
    motion_only = [(max(0.0, s - PAD_SEC), min(duration, e + PAD_SEC))
                   for s, e, label in collapsed if label]
    # Merge any motion segments that now overlap after padding.
    motion_only.sort()
    padded = []
    for s, e in motion_only:
        if padded and s <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(padded[-1][1], e))
        else:
            padded.append((s, e))

    final = []
    cursor = 0.0
    for s, e in padded:
        if s > cursor:
            final.append((cursor, s, False))
        final.append((s, e, True))
        cursor = e
    if cursor < duration:
        final.append((cursor, duration, False))

    # Drop segments below the minimum duration (fold into a neighbor instead of losing time).
    filtered = []
    for seg in final:
        s, e, label = seg
        if e - s < MIN_SEGMENT_SEC and filtered:
            filtered[-1] = (filtered[-1][0], e, filtered[-1][2])
        else:
            filtered.append(seg)

    return filtered


def cut_segment(src: Path, start: float, end: float, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
         "-i", str(src), "-c", "copy", str(dest)],
        check=True,
    )


def cut_and_verify(src: Path, start: float, end: float, dest: Path, max_attempts: int = 2) -> bool:
    """Cut a segment and decode-verify it while src (the raw file) is still
    around, retrying the cut from src if it fails -- ffprobe alone can miss
    corruption, and some failures are transient (an interrupted ffmpeg, a
    disk hiccup) rather than corruption actually in src. Only gives up (and
    deletes dest) after max_attempts identical failures, which means the
    corruption is really in src at that byte range, not the cut itself."""
    for attempt in range(1, max_attempts + 1):
        cut_segment(src, start, end, dest)
        err = decode_check(dest)
        if err is None:
            return True
        log_event({
            "event": "cut_retry" if attempt < max_attempts else "clip_corrupted",
            "clip": str(dest), "attempt": attempt, "error": err.splitlines()[0],
        })
    dest.unlink(missing_ok=True)
    return False


def process_video(video_path: Path):
    """Returns the list of active/idle clip paths this video produced (empty
    if the source was unrecoverably corrupted)."""
    stem = video_path.stem
    source = video_path
    recovered = None
    try:
        duration = ffprobe_duration(source)
    except subprocess.CalledProcessError:
        recovered = try_recover(video_path)
        if recovered is None:
            log_event({
                "event": "video_corrupted",
                "video": str(video_path),
                "note": "unrecoverable: source has no readable video stream (ffprobe and error-tolerant remux both failed)",
            })
            video_path.unlink()
            return []
        source = recovered
        duration = ffprobe_duration(source)

    samples = detect_motion_samples(source)
    segments = samples_to_segments(samples, duration)

    created = []
    active_count = idle_count = corrupted_count = 0
    active_time = idle_time = 0.0
    for i, (start, end, is_motion) in enumerate(segments):
        out_dir = ACTIVE_DIR if is_motion else IDLE_DIR
        dest = out_dir / f"{stem}_seg{i:03d}_{start:.1f}-{end:.1f}.mp4"
        if not cut_and_verify(source, start, end, dest):
            corrupted_count += 1
            continue  # unrecoverable even after a retry from source -- already logged
        created.append(dest)
        if is_motion:
            active_count += 1
            active_time += end - start
        else:
            idle_count += 1
            idle_time += end - start

    log_event({
        "event": "video_done",
        "video": str(video_path),
        "duration_sec": duration,
        "active_segments": active_count,
        "idle_segments": idle_count,
        "active_time_sec": active_time,
        "idle_time_sec": idle_time,
        "corrupted_segments": corrupted_count,
        "recovered": recovered is not None,
    })
    video_path.unlink()
    if recovered is not None:
        recovered.unlink(missing_ok=True)
    return created


def process_zip(zip_path: Path):
    """Returns the list of active/idle clip paths produced from this zip."""
    extract_dir = RAW_DIR / zip_path.stem
    extract_dir.mkdir(parents=True, exist_ok=True)
    log_event({"event": "extract_start", "zip": str(zip_path)})
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    log_event({"event": "extract_done", "zip": str(zip_path)})

    created = []
    for video_path in sorted(extract_dir.rglob("*.mp4")):
        try:
            log_event({"event": "video_start", "video": str(video_path)})
            created.extend(process_video(video_path))
        except Exception as e:
            log_event({"event": "video_error", "video": str(video_path), "error": str(e)})

    # Clean up now-empty extraction tree.
    for p in sorted(extract_dir.rglob("*"), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    try:
        extract_dir.rmdir()
    except OSError:
        pass
    log_event({"event": "zip_done", "zip": str(zip_path)})
    return created


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("zips", nargs="*", help="Zip files to process")
    parser.add_argument("--all", action="store_true", help="Process all Camera Footage-*.zip in cwd")
    args = parser.parse_args()

    if args.all:
        zips = sorted(ROOT.glob("Camera Footage-*.zip"))
    else:
        zips = [Path(z) for z in args.zips]

    if not zips:
        print("No zip files specified.", file=sys.stderr)
        sys.exit(1)

    for zip_path in zips:
        process_zip(zip_path)


if __name__ == "__main__":
    main()
