#!/opt/anaconda3/bin/python3
"""Concatenate every clip in ./active in chronological order and split the
result into fixed 10-minute files in ./active_merged.

Filenames look like: <base>_segNNN_<start>-<end>.mp4, where <base> is either
    Camera_YYYY-MM-DD_HH-MM-SS   (most files)
    cam-YYYYMMDD-HHMMSS          (one legacy naming variant)
and <start> is the offset in seconds into that base recording. Chronological
order = base timestamp + start offset.
"""
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE_DIR = ROOT / "active_tmp"
MERGED_DIR = ROOT / "active"
CHUNK_SEC = 1800  # 30 minutes

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


def next_local_index():
    """Merged files in ./active are no longer deleted after upload, so a
    fresh run must continue the numbering rather than restart at 000 --
    otherwise ffmpeg's segment muxer would silently overwrite whatever from
    a previous run is still sitting there."""
    indices = [int(m.group(1)) for p in MERGED_DIR.glob("active_part_*.mp4")
               if (m := PART_RE.match(p.name))]
    return max(indices, default=-1) + 1


def main():
    """Returns the list of newly created merged part paths (empty if there
    was nothing in ./active_tmp to merge)."""
    clips = sorted(ACTIVE_DIR.glob("*.mp4"), key=sort_key)
    if not clips:
        print("No clips found in ./active_tmp")
        return []

    print(f"Merging {len(clips)} clips in chronological order...")
    MERGED_DIR.mkdir(exist_ok=True)

    concat_list = ROOT / "active_concat_list.txt"
    with concat_list.open("w") as f:
        for clip in clips:
            escaped = str(clip.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    start_index = next_local_index()
    out_pattern = str(MERGED_DIR / "active_part_%03d.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", "-map", "0",
         "-f", "segment", "-segment_time", str(CHUNK_SEC),
         "-segment_start_number", str(start_index), "-reset_timestamps", "1",
         out_pattern],
        check=True,
    )
    concat_list.unlink()

    new_parts = [p for p in sorted(MERGED_DIR.glob("active_part_*.mp4"))
                 if (m := PART_RE.match(p.name)) and int(m.group(1)) >= start_index]
    print(f"Wrote {len(new_parts)} merged {CHUNK_SEC // 60}-minute files to {MERGED_DIR}")
    return new_parts


if __name__ == "__main__":
    main()
