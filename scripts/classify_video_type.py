"""
classify_video_type.py — Read-only video type classifier for Skool vaults.

Sampling strategy:
    For each video, 5 segments of 10 s are drawn via stratified-random positions:
    one per 20 % slice of the duration (≈ 10/30/50/70/90 %) with ±5 % jitter.
    Videos shorter than 60 s fall back to min(5, floor(duration/10)) evenly-spaced
    segments of 10 s each.

    Two ffmpeg passes per segment (single-process, releases GIL between them):
      Pass 1: signalstats   → YDIF (luma frame-diff) over the 10 s window
      Pass 2: edgedetect → signalstats  → YAVG (mean pixel of edge-detected frame)
    Scene cuts are counted from the stderr frame-select output of pass 1.

Output: <vault_root>/.vault-notes/video-types.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Thresholds (module-level constants — easy to tune)
# ---------------------------------------------------------------------------

# edge_density (YAVG of edge-detected frame, 0–255 scale)
EDGE_SLIDESHOW_MIN = 75      # slides have very high edge content (text, lines)
EDGE_HANDON_MIN = 22         # screencast has moderate edges (code, UI chrome)
EDGE_TALKING_MAX = 70        # talking heads have lower edge content (face, blur bg)

# diff_mean (YDIF: mean luma frame-to-frame difference, 0–255 scale)
DIFF_MOTION_MIN = 1.5        # moving content (talking heads with visible motion)
DIFF_STATIC_MAX = 1.5        # static content (slides, screencasts with few cursor moves)

# scenes_per_min
SCENES_HIGH = 3.0            # frequent cuts → NOT a slide-only segment
SCENES_LOW = 1.0             # very few cuts → slide or screencast steady content

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"
SEGMENT_LEN = 10             # seconds per segment
N_SEGMENTS = 5
SEGMENT_TIMEOUT = 60         # seconds; generous for large I-frame seeks


def probe_video(path: Path) -> dict:
    """Return {duration_s, codec, size_mb} or raise on failure."""
    cmd = [
        FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.decode(errors='replace')[:200]}")
    data = json.loads(r.stdout)
    duration = float(data["format"]["duration"])
    size_mb = round(int(data["format"]["size"]) / 1e6, 1)
    codec = "unknown"
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            codec = s.get("codec_name", "unknown")
            break
    return {"duration_s": duration, "codec": codec, "size_mb": size_mb}


def segment_positions(duration: float) -> list[float]:
    """Return list of segment start times (seconds)."""
    max_start = max(0.0, duration - SEGMENT_LEN)
    if duration < 60:
        n = max(1, min(N_SEGMENTS, int(duration // SEGMENT_LEN)))
        if n == 1:
            return [0.0]
        step = max_start / (n - 1) if n > 1 else 0
        return [min(i * step, max_start) for i in range(n)]

    positions = []
    for i in range(N_SEGMENTS):
        # Centre of each 20 % slice
        slice_centre = (i + 0.5) / N_SEGMENTS
        # ±5 % jitter
        jitter = random.uniform(-0.05, 0.05)
        frac = max(0.0, min(1.0, slice_centre + jitter))
        start = frac * max_start
        positions.append(round(start, 2))
    return positions


def _parse_signalstats_stdout(stdout: bytes) -> dict[str, Optional[float]]:
    """Parse metadata=print:file=- output; return mean YDIF and mean YAVG."""
    ydif_vals: list[float] = []
    yavg_vals: list[float] = []
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("lavfi.signalstats.YDIF="):
            try:
                ydif_vals.append(float(line.split("=", 1)[1]))
            except ValueError:
                pass
        elif line.startswith("lavfi.signalstats.YAVG="):
            try:
                yavg_vals.append(float(line.split("=", 1)[1]))
            except ValueError:
                pass
    return {
        "ydif_mean": round(sum(ydif_vals) / len(ydif_vals), 4) if ydif_vals else None,
        "yavg_mean": round(sum(yavg_vals) / len(yavg_vals), 4) if yavg_vals else None,
    }


def _count_scene_frames(stderr: bytes) -> int:
    """Count lines that indicate a selected scene frame from select=gt(scene,…)."""
    count = 0
    for line in stderr.decode(errors="replace").splitlines():
        if line.strip().startswith("pts_time:"):
            count += 1
    return count


def analyze_segment(path: Path, start: float) -> dict:
    """
    Run two ffmpeg passes over [start, start+10s] and return per-segment metrics.

    Pass 1: signalstats  →  YDIF (diff_mean)
    Pass 2: edgedetect → signalstats  →  YAVG (edge_density)
    Scene count comes from a select filter on the pass-1 stderr.
    """
    # --- Pass 1: luma diff + scene count ---
    # Scene detection via select filter writing to stderr (vframe metadata → stdout)
    # We run signalstats for YDIF in the same pass.
    cmd1 = [
        FFMPEG, "-ss", str(start), "-i", str(path),
        "-t", str(SEGMENT_LEN),
        "-vf", "signalstats,metadata=print:file=-",
        "-an", "-f", "null", "-",
        "-nostats", "-loglevel", "info",
    ]
    r1 = subprocess.run(cmd1, capture_output=True, timeout=SEGMENT_TIMEOUT)
    stats1 = _parse_signalstats_stdout(r1.stdout)
    diff_mean = stats1["ydif_mean"]

    # Count scene cuts from a separate stderr-only invocation (simpler parsing)
    cmd_scene = [
        FFMPEG, "-ss", str(start), "-i", str(path),
        "-t", str(SEGMENT_LEN),
        "-vf", "select='gt(scene,0.3)',metadata=print:file=-",
        "-vsync", "vfr", "-an", "-f", "null", "-",
        "-nostats", "-loglevel", "quiet",
    ]
    r_scene = subprocess.run(cmd_scene, capture_output=True, timeout=SEGMENT_TIMEOUT)
    scene_frames = 0
    for line in r_scene.stdout.decode(errors="replace").splitlines():
        if line.strip().startswith("pts_time:"):
            scene_frames += 1
    scenes_per_min = round(scene_frames / (SEGMENT_LEN / 60), 2)

    # --- Pass 2: edge density ---
    cmd2 = [
        FFMPEG, "-ss", str(start), "-i", str(path),
        "-t", str(SEGMENT_LEN),
        "-vf", "edgedetect=mode=colormix:low=0.1:high=0.4,signalstats,metadata=print:file=-",
        "-an", "-f", "null", "-",
        "-nostats", "-loglevel", "quiet",
    ]
    r2 = subprocess.run(cmd2, capture_output=True, timeout=SEGMENT_TIMEOUT)
    stats2 = _parse_signalstats_stdout(r2.stdout)
    edge_density = stats2["yavg_mean"]

    seg_type = classify_segment(diff_mean, edge_density, scenes_per_min)

    return {
        "start_s": start,
        "end_s": round(start + SEGMENT_LEN, 2),
        "scenes_per_min": scenes_per_min,
        "diff_mean": diff_mean,
        "edge_density": edge_density,
        "faces": None,   # cv2 not installed; gracefully omitted
        "type": seg_type,
    }


def classify_segment(
    diff_mean: Optional[float],
    edge_density: Optional[float],
    scenes_per_min: float,
) -> str:
    """Map per-segment metrics to a type label."""
    if diff_mean is None or edge_density is None:
        return "mixed"

    # Slideshow: very high edges (text, slide graphics), very static (low diff)
    if edge_density >= EDGE_SLIDESHOW_MIN and diff_mean < DIFF_MOTION_MIN:
        return "slideshow"

    # Hands-on / screencast: moderate-to-high edges (code, UI), very static
    if edge_density >= EDGE_HANDON_MIN and diff_mean < DIFF_STATIC_MAX and scenes_per_min < SCENES_LOW:
        return "handson"

    # Talking head: visible motion, lower edge content
    if diff_mean >= DIFF_MOTION_MIN and edge_density < EDGE_TALKING_MAX:
        return "talking"

    return "mixed"


def aggregate_segments(segments: list[dict]) -> dict:
    """Derive dominant_type, confidence, profile, and ratios from per-segment results."""
    types = [s["type"] for s in segments if "type" in s]
    if not types:
        return {
            "dominant_type": "mixed",
            "confidence": 0.0,
            "profile": "unknown",
            "ratios": {"talking": 0.0, "slideshow": 0.0, "handson": 0.0, "mixed": 1.0},
        }
    n = len(types)
    counts = {"talking": 0, "slideshow": 0, "handson": 0, "mixed": 0}
    for t in types:
        counts[t] = counts.get(t, 0) + 1

    dominant = max(counts, key=lambda k: counts[k])
    dominant_count = counts[dominant]
    confidence = round(dominant_count / n, 4)

    # Override to "mixed" if no type has majority (≥3/5)
    if dominant_count < 3 and n >= 5:
        dominant = "mixed"

    # Profile: "pure" if ≥80 % dominant, else "mixed"
    profile = "pure" if confidence >= 0.8 else "mixed"

    ratios = {k: round(v / n, 4) for k, v in counts.items()}
    return {
        "dominant_type": dominant,
        "confidence": confidence,
        "profile": profile,
        "ratios": ratios,
    }


def process_video(path: Path, vault_root: Path) -> dict:
    """Classify one video. Returns a result dict (may contain 'error' key)."""
    rel = str(path.relative_to(vault_root))
    try:
        info = probe_video(path)
        duration = info["duration_s"]
        positions = segment_positions(duration)
        segments = []
        for start in positions:
            try:
                seg = analyze_segment(path, start)
            except subprocess.TimeoutExpired:
                seg = {"start_s": start, "error": "timeout"}
            except Exception as exc:
                seg = {"start_s": start, "error": str(exc)[:200]}
            segments.append(seg)

        agg = aggregate_segments([s for s in segments if "error" not in s])
        return {
            "path": rel,
            "duration_s": round(duration, 2),
            "size_mb": info["size_mb"],
            "codec": info["codec"],
            "segments": segments,
            **agg,
        }
    except Exception as exc:
        return {"path": rel, "error": str(exc)[:400]}


def classify_vault(
    vault_root: Path,
    output_path: Optional[Path],
    concurrency: int,
    limit: Optional[int],
    verbose: bool,
) -> None:
    videos = sorted(vault_root.rglob("video.mp4"))
    if limit:
        videos = videos[:limit]

    total = len(videos)
    if total == 0:
        print(f"[{vault_root.name}] No video.mp4 files found.", file=sys.stderr)
        return

    print(f"[{vault_root.name}] Classifying {total} video(s) with concurrency={concurrency}…",
          file=sys.stderr)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(process_video, v, vault_root): v for v in videos}
        done = 0
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            results.append(result)
            if verbose:
                p = result.get("path", "?")
                if "error" in result:
                    print(f"  [{done}/{total}] ERROR  {p}: {result['error']}", file=sys.stderr)
                else:
                    t = result.get("dominant_type", "?")
                    conf = result.get("confidence", 0)
                    print(f"  [{done}/{total}] {t:<10} conf={conf:.0%}  {p}", file=sys.stderr)

    # Sort by path for deterministic output
    results.sort(key=lambda r: r.get("path", ""))

    # Summary
    by_type: dict[str, int] = {"talking": 0, "slideshow": 0, "handson": 0, "mixed": 0}
    pure = 0
    valid = 0
    for r in results:
        if "error" in r:
            continue
        valid += 1
        dt = r.get("dominant_type", "mixed")
        by_type[dt] = by_type.get(dt, 0) + 1
        if r.get("profile") == "pure":
            pure += 1

    summary = {
        "total": total,
        "processed": valid,
        "errors": total - valid,
        "by_type": by_type,
        "pure_ratio": round(pure / valid, 4) if valid else 0.0,
    }

    thresholds = {
        "EDGE_SLIDESHOW_MIN": EDGE_SLIDESHOW_MIN,
        "EDGE_HANDON_MIN": EDGE_HANDON_MIN,
        "EDGE_TALKING_MAX": EDGE_TALKING_MAX,
        "DIFF_MOTION_MIN": DIFF_MOTION_MIN,
        "DIFF_STATIC_MAX": DIFF_STATIC_MAX,
        "SCENES_HIGH": SCENES_HIGH,
        "SCENES_LOW": SCENES_LOW,
    }

    output = {
        "vault_root": str(vault_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": thresholds,
        "videos": results,
        "summary": summary,
    }

    if output_path is None:
        notes_dir = vault_root / ".vault-notes"
        notes_dir.mkdir(exist_ok=True)
        output_path = notes_dir / "video-types.json"

    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"[{vault_root.name}] Written → {output_path}", file=sys.stderr)
    print(f"  Summary: {summary}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify video.mp4 files in a Skool vault as talking/slideshow/handson/mixed.",
    )
    parser.add_argument("vault_roots", nargs="+", metavar="VAULT_ROOT",
                        help="One or more vault root directories.")
    parser.add_argument("--concurrency", type=int,
                        default=min(4, max(1, (os.cpu_count() or 4) // 2)),
                        help="Max parallel ffmpeg workers (default: 4).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N videos (for testing).")
    parser.add_argument("--output", type=Path, default=None,
                        help="Override output JSON path (used only with a single vault).")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-video results as they complete.")
    args = parser.parse_args()

    if args.output and len(args.vault_roots) > 1:
        parser.error("--output can only be used with a single vault root.")

    for vr in args.vault_roots:
        vault_root = Path(vr).resolve()
        if not vault_root.is_dir():
            print(f"ERROR: not a directory: {vault_root}", file=sys.stderr)
            sys.exit(1)
        classify_vault(
            vault_root=vault_root,
            output_path=args.output,
            concurrency=args.concurrency,
            limit=args.limit,
            verbose=args.verbose,
        )


if __name__ == "__main__":
    main()
