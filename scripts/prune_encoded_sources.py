#!/usr/bin/env python3
"""
Delete original video.mp4 files that have a verified HEVC copy, and point the
offline viewer (index.html) at video.hevc.mp4.

A source is deleted only when ALL of these hold:
  - video.hevc.mp4 and its video.hevc.json sidecar exist
  - ffprobe reads a video stream from video.hevc.mp4
  - the HEVC duration is within --tolerance seconds of the source duration

USAGE:
    python scripts/prune_encoded_sources.py <vault-root> [--dry-run]

Exit code 1 when any encoded copy fails verification (sources are kept).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


def probe(path: Path) -> Optional[tuple[float, str]]:
    """Return (duration_sec, first video codec) or None when unreadable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(r.stdout or "{}")
        codec = next((s["codec_name"] for s in data.get("streams", []) if s.get("codec_type") == "video"), "")
        return float(data["format"]["duration"]), codec
    except (subprocess.TimeoutExpired, ValueError, KeyError, OSError):
        return None


def repoint_viewer(lesson_dir: Path, dry_run: bool) -> bool:
    html = lesson_dir / "index.html"
    if not html.exists():
        return False
    text = html.read_text(encoding="utf-8", errors="ignore")
    if 'src="video.mp4"' not in text:
        return False
    if not dry_run:
        html.write_text(text.replace('src="video.mp4"', 'src="video.hevc.mp4"'), encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--tolerance", type=float, default=1.5, help="max duration delta in seconds")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    pruned = kept = bad = repointed = 0
    freed = 0
    for src in sorted(root.rglob("video.mp4")):
        d = src.parent
        hevc, sidecar = d / "video.hevc.mp4", d / "video.hevc.json"
        if not (hevc.exists() and sidecar.exists()):
            kept += 1
            continue
        a, b = probe(src), probe(hevc)
        if b is None or b[1] != "hevc" or a is None or abs(a[0] - b[0]) > args.tolerance:
            bad += 1
            print(f"VERIFY FAIL: {src.relative_to(root)} src={a} hevc={b}")
            continue
        size = src.stat().st_size
        if not args.dry_run:
            src.unlink()
        pruned += 1
        freed += size
        print(f"pruned: {src.relative_to(root)} ({size / 1e9:.2f} GB)")

    for hevc in sorted(root.rglob("video.hevc.mp4")):
        if not (hevc.parent / "video.mp4").exists() and repoint_viewer(hevc.parent, args.dry_run):
            repointed += 1

    tag = " (dry run)" if args.dry_run else ""
    print(f"pruned {pruned} sources, freed {freed / 1e9:.2f} GB, kept {kept} not yet encoded, "
          f"{bad} failed verification, repointed {repointed} viewers{tag}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
