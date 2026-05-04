"""
reencode_videos.py — Re-encode Skool vault videos to HEVC/H.265 MP4.

Encoding policy table:
  dominant_type  target_height  CRF
  talking        720            28   (face content; lower res fine)
  slideshow      1080 (cap)     26   (text sharpness matters)
  handson        1080 (cap)     26   (code sharpness matters)
  mixed          1080 (cap)     27
  unknown/error  1080 (cap)     27   (warn + fallback)

Never upscales. Scale filter uses lanczos, forces even width (-2).
Audio: AAC LC 128k stereo. Container: MP4 with faststart + hvc1 tag
(required for macOS Finder/QuickLook/QuickTime native preview).

Idempotency: skips re-encode when .hevc.mp4 + .hevc.json sidecar exist
and source fingerprint (sha256 of first 1 MB, size, mtime) + policy params
all match. Writes to .hevc.mp4.tmp then renames atomically; sidecar written
only after successful rename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Policy table
# ---------------------------------------------------------------------------

POLICY: dict[str, dict] = {
    "talking":   {"target_h": 720,  "crf": 28},
    "slideshow": {"target_h": 1080, "crf": 26},
    "handson":   {"target_h": 1080, "crf": 26},
    "mixed":     {"target_h": 1080, "crf": 27},
    "_default":  {"target_h": 1080, "crf": 27},
}

# ---------------------------------------------------------------------------
# ffmpeg / ffprobe helpers
# ---------------------------------------------------------------------------

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def check_libx265() -> str:
    """Fail fast if ffmpeg lacks libx265. Returns ffmpeg version string."""
    try:
        r = subprocess.run(
            [FFMPEG, "-version"], capture_output=True, timeout=10
        )
        version_line = r.stdout.decode(errors="replace").splitlines()[0] if r.stdout else "unknown"
    except Exception as e:
        print(f"ERROR: cannot run ffmpeg: {e}", file=sys.stderr)
        sys.exit(1)

    r2 = subprocess.run(
        [FFMPEG, "-encoders"], capture_output=True, timeout=10
    )
    encoders = r2.stdout.decode(errors="replace") + r2.stderr.decode(errors="replace")
    if "libx265" not in encoders:
        print("ERROR: ffmpeg does not have libx265 support.", file=sys.stderr)
        sys.exit(1)
    return version_line.strip()


def probe_video(path: Path) -> dict:
    """Return {width, height, duration_s, codec} via ffprobe."""
    cmd = [
        FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.decode(errors='replace')[:300]}")
    data = json.loads(r.stdout)
    duration = float(data["format"].get("duration", 0))
    width = height = 0
    codec = "unknown"
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            width = int(s.get("width", 0))
            height = int(s.get("height", 0))
            codec = s.get("codec_name", "unknown")
            break
    return {"width": width, "height": height, "duration_s": duration, "codec": codec}


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def sha256_head(path: Path, nbytes: int = 1_048_576) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        chunk = f.read(nbytes)
    h.update(chunk)
    return h.hexdigest()


def source_fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {
        "source_sha256_head": sha256_head(path),
        "source_size": stat.st_size,
        "source_mtime": stat.st_mtime,
    }


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------

def resolve_policy(dominant_type: Optional[str], preset: str) -> tuple[int, int, str]:
    """Return (target_h, crf, preset). Falls back to _default for unknown types."""
    pol = POLICY.get(dominant_type or "_default", POLICY["_default"])
    return pol["target_h"], pol["crf"], preset


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------

def should_skip(
    src: Path,
    out_mp4: Path,
    sidecar: Path,
    target_h: int,
    crf: int,
    classification_type: str,
    force: bool,
) -> bool:
    if force:
        return False
    if not out_mp4.exists() or not sidecar.exists():
        return False
    try:
        sc = json.loads(sidecar.read_text())
    except Exception:
        return False
    fp = source_fingerprint(src)
    if (
        sc.get("source_sha256_head") == fp["source_sha256_head"]
        and sc.get("source_size") == fp["source_size"]
        and sc.get("source_mtime") == fp["source_mtime"]
        and sc.get("classification_type") == classification_type
        and sc.get("target_height") == target_h
        and sc.get("crf") == crf
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def build_scale_filter(target_h: int, source_h: int) -> str:
    # Never upscale
    effective_h = min(target_h, source_h) if source_h > 0 else target_h
    # -2 keeps even width; lanczos for sharpness
    return f"scale=-2:min({effective_h}\\,ih):flags=lanczos"


def compute_thread_budget(concurrency: int) -> tuple[int, int]:
    """
    Return (pool_size, frame_threads) for x265 given concurrency.
    Honours REENCODE_THREADS_PER_INSTANCE env var as an escape hatch.
    """
    override = os.environ.get("REENCODE_THREADS_PER_INSTANCE")
    if override:
        pool_size = max(1, int(override))
    else:
        total = os.cpu_count() or 8
        reserved = 1  # headroom for OS / parent python / ffmpeg muxing
        pool_size = max(1, (total - reserved) // concurrency)
    frame_threads = min(6, max(1, pool_size // 2))
    return pool_size, frame_threads


def encode_video(
    src: Path,
    out_mp4: Path,
    sidecar: Path,
    target_h: int,
    crf: int,
    preset: str,
    source_h: int,
    duration_s: float,
    classification_type: str,
    ffmpeg_version: str,
    verbose: bool,
    pool_size: int = 0,
    frame_threads: int = 0,
) -> dict:
    """
    Run ffmpeg. Returns dict with 'ok', 'elapsed_s', 'error'.
    Writes to .tmp then renames. Cleans up .tmp on failure.
    """
    tmp = out_mp4.with_name("video.hevc.tmp.mp4")
    # Clean up leftover tmp
    if tmp.exists():
        tmp.unlink()

    scale_f = build_scale_filter(target_h, source_h)
    timeout = max(300, int(duration_s * 4)) if duration_s > 0 else 3600

    x265_params = f"pools={pool_size}:frame-threads={frame_threads}" if pool_size else ""
    cmd = [
        FFMPEG, "-y",
        "-threads", str(pool_size) if pool_size else "0",
        "-i", str(src),
        "-c:v", "libx265",
        "-crf", str(crf),
        "-preset", preset,
        "-tag:v", "hvc1",
        "-vf", scale_f,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
    ]
    if x265_params:
        cmd += ["-x265-params", x265_params]
    cmd.append(str(tmp))

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr_lines: list[str] = []
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace")
                stderr_lines.append(line)
                # Keep last 100 lines to save memory
                if len(stderr_lines) > 100:
                    stderr_lines = stderr_lines[-100:]
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            if tmp.exists():
                tmp.unlink()
            return {"ok": False, "elapsed_s": time.monotonic() - t0,
                    "error": "timeout", "stderr_tail": stderr_lines[-50:]}
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        if tmp.exists():
            tmp.unlink()
        raise

    elapsed = time.monotonic() - t0

    if proc.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        return {"ok": False, "elapsed_s": elapsed, "error": f"ffmpeg exit {proc.returncode}",
                "stderr_tail": stderr_lines[-50:]}

    # Atomic rename
    tmp.rename(out_mp4)

    # Write sidecar
    fp = source_fingerprint(src)
    out_size = out_mp4.stat().st_size
    src_size = fp["source_size"]
    sidecar_data = {
        **fp,
        "classification_type": classification_type,
        "target_height": target_h,
        "crf": crf,
        "preset": preset,
        "threads_per_instance": pool_size,
        "frame_threads": frame_threads,
        "encoded_at": datetime.now(timezone.utc).isoformat(),
        "output_size": out_size,
        "compression_ratio": round(out_size / src_size, 4) if src_size else None,
        "ffmpeg_version": ffmpeg_version,
    }
    sidecar.write_text(json.dumps(sidecar_data, indent=2))

    return {"ok": True, "elapsed_s": elapsed, "output_size": out_size,
            "source_size": src_size, "stderr_tail": []}


# ---------------------------------------------------------------------------
# Per-vault classification lookup
# ---------------------------------------------------------------------------

def load_classification(vault_root: Path) -> dict[str, dict]:
    """Return {relative_path: record} from video-types.json, or {}."""
    json_path = vault_root / ".vault-notes" / "video-types.json"
    if not json_path.exists():
        return {}
    try:
        data = json.loads(json_path.read_text())
        videos = data.get("videos", [])
        return {v["path"]: v for v in videos if "path" in v and "error" not in v}
    except Exception as e:
        print(f"WARNING: could not parse {json_path}: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Work item
# ---------------------------------------------------------------------------

DRAIN_SENTINEL = Path("/tmp/reencode_drain")


def process_one(
    src: Path,
    vault_root: Path,
    classification_map: dict[str, dict],
    preset: str,
    force: bool,
    dry_run: bool,
    delete_source: bool,
    ffmpeg_version: str,
    verbose: bool,
    pool_size: int = 0,
    frame_threads: int = 0,
) -> dict:
    """Process a single video. Returns status dict."""
    rel = str(src.relative_to(vault_root))

    # Graceful drain: when /tmp/reencode_drain exists, new tasks skip immediately.
    # In-flight tasks (already past this check) finish normally, so no progress is lost.
    if DRAIN_SENTINEL.exists():
        return {"rel": rel, "status": "drained"}
    out_mp4 = src.with_name("video.hevc.mp4")
    sidecar = src.with_name("video.hevc.json")
    err_log = src.with_name("video.hevc.error.log")

    # Probe source
    try:
        probe = probe_video(src)
    except Exception as e:
        return {"rel": rel, "status": "probe_error", "error": str(e)}

    source_h = probe["height"]
    source_w = probe["width"]
    duration_s = probe["duration_s"]
    src_codec = probe["codec"]
    src_size = src.stat().st_size

    # Classification lookup
    rec = classification_map.get(rel, {})
    dominant_type = rec.get("dominant_type") if rec else None
    if dominant_type not in ("talking", "slideshow", "handson", "mixed"):
        dominant_type = "_default"

    target_h, crf, preset_used = resolve_policy(dominant_type, preset)
    display_type = dominant_type if dominant_type != "_default" else "unknown"

    if dominant_type == "_default" and verbose:
        print(f"  WARNING: no classification for {rel}, using fallback policy", file=sys.stderr)

    # Idempotency
    if should_skip(src, out_mp4, sidecar, target_h, crf, display_type, force):
        deleted = False
        if delete_source and src.exists():
            try:
                src.unlink()
                deleted = True
            except OSError as e:
                print(f"  WARN: could not delete {src}: {e}", file=sys.stderr)
        return {"rel": rel, "status": "skipped", "source_size": src_size,
                "dominant_type": display_type, "source_deleted": deleted}

    src_mb = src_size / 1e6

    if dry_run:
        effective_h = min(target_h, source_h) if source_h > 0 else target_h
        print(f"  [DRY-RUN] {rel}")
        print(f"    type={display_type}, source={source_w}x{source_h} {src_codec}, "
              f"{src_mb:.0f}MB → {effective_h}p CRF{crf} {preset_used}")
        return {"rel": rel, "status": "dry_run", "source_size": src_size,
                "dominant_type": display_type, "target_h": target_h, "crf": crf}

    result = encode_video(
        src=src,
        out_mp4=out_mp4,
        sidecar=sidecar,
        target_h=target_h,
        crf=crf,
        preset=preset_used,
        source_h=source_h,
        duration_s=duration_s,
        classification_type=display_type,
        ffmpeg_version=ffmpeg_version,
        verbose=verbose,
        pool_size=pool_size,
        frame_threads=frame_threads,
    )

    if result["ok"]:
        elapsed = result["elapsed_s"]
        out_size = result["output_size"]
        if err_log.exists():
            err_log.unlink()
        if delete_source:
            src.unlink()
        return {"rel": rel, "status": "encoded", "source_size": src_size,
                "output_size": out_size, "elapsed_s": elapsed,
                "dominant_type": display_type}
    else:
        err = result["error"]
        stderr_tail = result.get("stderr_tail", [])
        err_log.write_text("".join(stderr_tail[-50:]))
        return {"rel": rel, "status": "error", "error": err,
                "source_size": src_size, "dominant_type": display_type}


# ---------------------------------------------------------------------------
# Main vault processing
# ---------------------------------------------------------------------------

def process_vault(
    vault_root: Path,
    preset: str,
    concurrency: int,
    dry_run: bool,
    limit: Optional[int],
    types_filter: Optional[set[str]],
    force: bool,
    delete_source: bool,
    verbose: bool,
    ffmpeg_version: str,
    index_start: int,
    total_across_vaults: int,
    pool_size: int = 0,
    frame_threads: int = 0,
) -> list[dict]:
    videos = sorted(vault_root.rglob("video.mp4"))
    classification_map = load_classification(vault_root)

    if not classification_map:
        print(f"  WARNING: no video-types.json found at {vault_root / '.vault-notes'} — "
              f"all videos will use fallback policy (1080p CRF27)", file=sys.stderr)

    # Filter by types if requested
    if types_filter:
        filtered = []
        for v in videos:
            rel = str(v.relative_to(vault_root))
            rec = classification_map.get(rel, {})
            dt = rec.get("dominant_type", "_default")
            if dt not in types_filter and "_default" not in types_filter:
                if dt not in types_filter:
                    continue
            filtered.append(v)
        videos = filtered

    if limit:
        videos = videos[:limit]

    total = len(videos)
    if total == 0:
        print(f"[{vault_root.name}] No videos to process.")
        return []

    # Header
    by_type: dict[str, int] = {}
    total_src_bytes = 0
    for v in videos:
        rel = str(v.relative_to(vault_root))
        rec = classification_map.get(rel, {})
        dt = rec.get("dominant_type", "unknown")
        by_type[dt] = by_type.get(dt, 0) + 1
        try:
            total_src_bytes += v.stat().st_size
        except OSError:
            pass

    print(f"\n{'='*70}")
    print(f"Vault: {vault_root}")
    print(f"  Videos:  {total}")
    print(f"  Source:  {total_src_bytes/1e9:.2f} GB")
    print(f"  By type: {by_type}")
    print(f"  Preset:  {preset} | Concurrency: {concurrency} | Force: {force}")
    print(f"{'='*70}")

    results: list[dict] = []
    done_count = index_start

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_meta = {}
        for i, v in enumerate(videos):
            rel = str(v.relative_to(vault_root))
            rec = classification_map.get(rel, {})
            dt = rec.get("dominant_type", "unknown")
            src_mb = 0
            try:
                src_mb = v.stat().st_size / 1e6
            except OSError:
                pass
            try:
                probe = probe_video(v)
                w, h, codec = probe["width"], probe["height"], probe["codec"]
                geo = f"{w}x{h} {codec}"
            except Exception:
                geo = "?"

            target_h, crf, _ = resolve_policy(
                dt if dt in POLICY else "_default", preset
            )

            fut = pool.submit(
                process_one,
                v, vault_root, classification_map, preset, force,
                dry_run, delete_source, ffmpeg_version, verbose,
                pool_size, frame_threads,
            )
            future_to_meta[fut] = {
                "rel": rel, "dt": dt, "geo": geo, "src_mb": src_mb,
                "target_h": target_h, "crf": crf, "idx": i,
            }

        for fut in as_completed(future_to_meta):
            done_count += 1
            meta = future_to_meta[fut]
            print(f"[{done_count:>4}/{total_across_vaults}] {meta['rel'][:80]}  "
                  f"({meta['dt']}, {meta['geo']}, {meta['src_mb']:.0f}MB) "
                  f"→ {meta['target_h']}p CRF{meta['crf']} ...")
            try:
                r = fut.result()
            except Exception as e:
                r = {"rel": meta["rel"], "status": "error", "error": str(e)}
                print(f"   EXCEPTION: {e}", file=sys.stderr)

            if r.get("status") == "encoded":
                elapsed = r.get("elapsed_s", 0)
                out_size = r.get("output_size", 0)
                src_size = r.get("source_size", 1)
                saved = src_size - out_size
                ratio = out_size / src_size if src_size else 1.0
                elapsed_str = f"{int(elapsed // 60)}m{int(elapsed % 60)}s"
                print(f"   done in {elapsed_str} → {out_size/1e6:.0f}MB "
                      f"({ratio*100:.0f}% of source, saved {saved/1e6:.0f}MB)")
            elif r.get("status") == "skipped":
                print(f"   skipped (already encoded)")
            elif r.get("status") == "drained":
                print(f"   drained (stop sentinel active)")
            elif r.get("status") == "error":
                print(f"   ERROR: {r.get('error','?')}", file=sys.stderr)

            results.append(r)

    return results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(all_results: list[dict]) -> None:
    total_src = sum(r.get("source_size", 0) for r in all_results)
    total_out = sum(r.get("output_size", 0) for r in all_results if r.get("status") == "encoded")
    encoded = [r for r in all_results if r.get("status") == "encoded"]
    skipped = [r for r in all_results if r.get("status") == "skipped"]
    errors  = [r for r in all_results if r.get("status") == "error"]
    dry     = [r for r in all_results if r.get("status") == "dry_run"]

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"  Encoded:  {len(encoded)}  |  Skipped: {len(skipped)}  |  "
          f"Errors: {len(errors)}  |  Dry-run: {len(dry)}")
    if encoded:
        src_enc = sum(r.get("source_size", 0) for r in encoded)
        saved = src_enc - total_out
        ratio = total_out / src_enc if src_enc else 1.0
        print(f"  Input:    {src_enc/1e9:.2f} GB  (encoded videos only)")
        print(f"  Output:   {total_out/1e9:.2f} GB")
        print(f"  Saved:    {saved/1e9:.2f} GB  ({(1-ratio)*100:.0f}%)")

        # By type
        by_type: dict[str, dict] = {}
        for r in encoded:
            dt = r.get("dominant_type", "unknown")
            if dt not in by_type:
                by_type[dt] = {"count": 0, "src": 0, "out": 0}
            by_type[dt]["count"] += 1
            by_type[dt]["src"] += r.get("source_size", 0)
            by_type[dt]["out"] += r.get("output_size", 0)
        print("  By type:")
        for dt, stats in sorted(by_type.items()):
            s = stats["src"]; o = stats["out"]
            pct = (1 - o/s)*100 if s else 0
            print(f"    {dt:<12} {stats['count']:>4} videos  "
                  f"{s/1e9:.2f}→{o/1e9:.2f} GB  saved {pct:.0f}%")

    if errors:
        print(f"  Errors ({len(errors)}):")
        for r in errors:
            print(f"    {r.get('rel','?')}: {r.get('error','?')}")
    print(f"{'='*70}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-encode Skool vault videos to HEVC (H.265) MP4.",
    )
    parser.add_argument("vault_roots", nargs="+", metavar="VAULT_ROOT")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="Max parallel ffmpeg workers (default: 2)")
    parser.add_argument("--preset", default="medium",
                        choices=["medium", "slow", "slower", "veryslow"],
                        help="x265 preset (default: medium)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print policy; no encoding")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only first N eligible videos")
    parser.add_argument("--types", default=None,
                        help="Comma-separated types to process: talking,slideshow,handson,mixed")
    parser.add_argument("--force", action="store_true",
                        help="Ignore sidecar cache; re-encode everything")
    parser.add_argument("--delete-source", action="store_true",
                        help="Delete source video.mp4 after successful encode (OPT-IN)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    ffmpeg_version = check_libx265()
    if args.verbose:
        print(f"ffmpeg: {ffmpeg_version}")

    pool_size, frame_threads = compute_thread_budget(args.concurrency)
    total_cores = os.cpu_count() or 8
    print(f"threads: {total_cores} cores detected, {args.concurrency} concurrent "
          f"x {pool_size} threads/instance (1 reserved for OS), "
          f"frame-threads={frame_threads}")

    types_filter: Optional[set[str]] = None
    if args.types:
        types_filter = {t.strip() for t in args.types.split(",")}

    vault_roots = [Path(v).resolve() for v in args.vault_roots]
    for vr in vault_roots:
        if not vr.is_dir():
            print(f"ERROR: not a directory: {vr}", file=sys.stderr)
            sys.exit(1)

    # Count total videos across all vaults for display
    total_all = 0
    for vr in vault_roots:
        total_all += len(list(vr.rglob("video.mp4")))

    all_results: list[dict] = []
    idx = 0
    for vr in vault_roots:
        results = process_vault(
            vault_root=vr,
            preset=args.preset,
            concurrency=args.concurrency,
            pool_size=pool_size,
            frame_threads=frame_threads,
            dry_run=args.dry_run,
            limit=args.limit,
            types_filter=types_filter,
            force=args.force,
            delete_source=args.delete_source,
            verbose=args.verbose,
            ffmpeg_version=ffmpeg_version,
            index_start=idx,
            total_across_vaults=total_all,
        )
        idx += len(results)
        all_results.extend(results)

    print_summary(all_results)


if __name__ == "__main__":
    main()
