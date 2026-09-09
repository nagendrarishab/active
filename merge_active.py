#!/opt/anaconda3/bin/python3
"""Concatenate clips in ./active_tmp/<source> in chronological order and
split the result into fixed 30-minute files in ./active/<source>, one
source at a time. Sources (one per configured SOURCE_FOLDER, or "manual"
for zip-dropped footage -- see process_footage.process_video) are never
mixed together: each gets its own merged output and its own
active_part_NNN numbering, so footage from different source folders never
ends up combined into the same video.

Filenames look like: <base>_segNNN_<start>-<end>.mp4, where <base> is either
    Camera_YYYY-MM-DD_HH-MM-SS   (most files)
    cam-YYYYMMDD-HHMMSS          (one legacy naming variant)
and <start> is the offset in seconds into that base recording. Chronological
order (within a source) = base timestamp + start offset.
"""
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

ACTIVE_DIR = ROOT / "active_tmp"
MERGED_DIR = ROOT / "active"
CHUNK_SEC = 1800  # 30 minutes

RCLONE_REMOTE = os.environ["RCLONE_REMOTE"]
DEST_FOLDER = os.environ["DEST_FOLDER"]

NAME_RE = re.compile(
    r"^(?P<base>Camera_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}|cam-\d{8}-\d{6})"
    r"_seg\d+_(?P<start>\d+(?:\.\d+)?)-\d+(?:\.\d+)?\.mp4$"
)


def parse_base_dt(base: str) -> datetime:
    if base.startswith("Camera_"):
        return datetime.strptime(base, "Camera_%Y-%m-%d_%H-%M-%S")
    return datetime.strptime(base, "cam-%Y%m%d-%H%M%S")


def sort_key(path: Path):
    m = NAME_RE.match(path.name)
    if not m:
        raise ValueError(f"Unrecognized active clip filename: {path.name}")
    base_dt = parse_base_dt(m.group("base"))
    start_offset = float(m.group("start"))
    return base_dt.timestamp() + start_offset


PART_RE = re.compile(r"^active_part_(\d+)\.mp4$")


def next_local_index(source: str):
    """Check Drive for the highest active_part_NNN.mp4 already uploaded for
    this source (DEST_FOLDER/<source> may not exist yet on Drive) and return
    the next consecutive index to use for this source's new merged files."""
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
    # Also check any local files not yet uploaded
    indices += [int(m.group(1)) for p in (MERGED_DIR / source).glob("active_part_*.mp4")
                if (m := PART_RE.match(p.name))]
    return max(indices, default=-1) + 1


def merge_source(source_dir: Path):
    """Merge one source's clips (./active_tmp/<source>) into 30-minute files
    in ./active/<source>. Returns the list of newly created part paths."""
    source = source_dir.name
    clips = sorted(source_dir.glob("*.mp4"), key=sort_key)
    if not clips:
        return []

    print(f"[{source}] merging {len(clips)} clips in chronological order...")
    out_dir = MERGED_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)

    concat_list = ROOT / f"active_concat_list_{source}.txt"
    with concat_list.open("w") as f:
        for clip in clips:
            escaped = str(clip.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    start_index = next_local_index(source)
    out_pattern = str(out_dir / "active_part_%03d.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", "-map", "0",
         "-f", "segment", "-segment_time", str(CHUNK_SEC),
         "-segment_start_number", str(start_index), "-reset_timestamps", "1",
         out_pattern],
        check=True,
    )
    concat_list.unlink()

    new_parts = [p for p in sorted(out_dir.glob("active_part_*.mp4"))
                 if (m := PART_RE.match(p.name)) and int(m.group(1)) >= start_index]
    print(f"[{source}] wrote {len(new_parts)} merged {CHUNK_SEC // 60}-minute files to {out_dir}")
    return new_parts


def main():
    """Merges each source's clips independently, never interleaving footage
    across sources. Returns the combined list of newly created merged part
    paths across all sources (empty if there was nothing in ./active_tmp)."""
    if not ACTIVE_DIR.exists():
        print("No clips found in ./active_tmp")
        return []

    source_dirs = sorted(p for p in ACTIVE_DIR.iterdir() if p.is_dir())
    if not source_dirs:
        print("No clips found in ./active_tmp")
        return []

    new_parts = []
    for source_dir in source_dirs:
        new_parts.extend(merge_source(source_dir))
    return new_parts


if __name__ == "__main__":
    main()
