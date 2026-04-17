#!/usr/bin/env python3
"""
Standalone video transcription tool for skool-downloader.

Walks a directory tree, finds video files, and writes a transcript.md next
to each one using parakeet-mlx (NVIDIA Parakeet-TDT via Apple Silicon / Metal GPU).

SETUP (one-time — requires Python ≥3.10):
    python3.12 -m venv .venv-transcribe-p312
    source .venv-transcribe-p312/bin/activate
    pip install -r scripts/transcribe_videos.requirements.txt
    # ffmpeg is required on PATH. Install via: brew install ffmpeg

USAGE:
    python scripts/transcribe_videos.py <root-dir> [options]
    python scripts/transcribe_videos.py downloads/makerschool --limit 5
    python scripts/transcribe_videos.py downloads/makerschool --force
    python scripts/transcribe_videos.py downloads/makerschool --dry-run

BACKEND:
    Default: parakeet-mlx (NVIDIA Parakeet-TDT-0.6B-v3) — ~60× real-time,
             CTC/TDT decoder, architecturally immune to repetition-loop
             hallucinations, English-only.
    Fallback: --backend whisper — uses mlx-whisper (requires Python 3.9 venv).
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Global state for graceful interrupt
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_sigint(signum, frame):  # noqa: ANN001
    global _shutdown_requested
    print("\n\n⚠️  SIGINT received — will finish current video then exit cleanly.", flush=True)
    _shutdown_requested = True


signal.signal(signal.SIGINT, _handle_sigint)


# ---------------------------------------------------------------------------
# Dependency checks (fast-fail before doing any work)
# ---------------------------------------------------------------------------

def check_ffmpeg() -> None:
    """Abort if ffmpeg is not on PATH."""
    if not shutil.which("ffmpeg"):
        sys.exit(
            "ERROR: ffmpeg not found on PATH.\n"
            "Install it with:  brew install ffmpeg\n"
            "Then re-run this script."
        )


def import_parakeet():
    """Import parakeet_mlx or abort with clear instructions."""
    try:
        from parakeet_mlx import from_pretrained  # noqa: PLC0415
        return from_pretrained
    except ImportError:
        sys.exit(
            "ERROR: parakeet_mlx is not installed (requires Python ≥3.10).\n"
            "Run:\n"
            "  python3.12 -m venv .venv-transcribe-p312\n"
            "  source .venv-transcribe-p312/bin/activate\n"
            "  pip install -r scripts/transcribe_videos.requirements.txt\n"
        )


def import_mlx_whisper():
    """Import mlx_whisper or abort with clear instructions (fallback backend)."""
    try:
        import mlx_whisper  # noqa: PLC0415
        return mlx_whisper
    except ImportError:
        sys.exit(
            "ERROR: mlx_whisper is not installed (whisper fallback backend).\n"
            "Run:\n"
            "  python3 -m venv .venv-transcribe\n"
            "  source .venv-transcribe/bin/activate\n"
            "  pip install mlx-whisper>=0.4.0\n"
        )


# ---------------------------------------------------------------------------
# Audio duration probe (ffprobe)
# ---------------------------------------------------------------------------

def probe_duration(video_path: Path) -> Optional[float]:
    """
    Return audio duration in seconds using ffprobe, or None on failure.
    Used to decide whether to enable local attention for long audio.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _strip_index_prefix(name: str) -> str:
    """Remove leading 'N-' prefix from a folder/file name (e.g. '1-Intro' -> 'Intro')."""
    return re.sub(r"^\d+-", "", name).strip()


def read_lesson_metadata(lesson_dir: Path) -> dict:
    """
    Return frontmatter fields from lesson.json if present, else fall back to
    folder-name heuristics.
    """
    json_path = lesson_dir / "lesson.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return {
                "lesson_title": data.get("title", _strip_index_prefix(lesson_dir.name)),
                "module_title": data.get("moduleTitle", ""),
                "lesson_id": data.get("lessonId", ""),
                "relative_path": data.get("relativePath", ""),
            }
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: derive from folder name
    return {
        "lesson_title": _strip_index_prefix(lesson_dir.name),
        "module_title": "",
        "lesson_id": "",
        "relative_path": "",
    }


# ---------------------------------------------------------------------------
# Paragraph grouping (works with both Whisper segments and Parakeet sentences)
# ---------------------------------------------------------------------------

def build_paragraphs(segments: list, target_chars: int = 600, max_sentences: int = 5) -> str:
    """
    Group segments/sentences into human-readable paragraphs.

    Accepts any list of objects/dicts that have a 'text' attribute or key.
    Accumulates items until ~target_chars or ~max_sentences, then starts a new
    paragraph.

    Compatible with:
    - Whisper segments: dicts with {"text": str, "start": float, "end": float}
    - Parakeet AlignedSentence objects: .text, .start, .end attributes
    """
    if not segments:
        return ""

    paragraphs: list[str] = []
    current_parts: list[str] = []
    current_chars = 0
    current_sentences = 0

    # Count approximate sentence endings in a chunk of text.
    _SENTENCE_END = re.compile(r"[.!?]\s")

    for seg in segments:
        # Support both dict (whisper) and object (parakeet) access
        if isinstance(seg, dict):
            raw = seg.get("text", "").strip()
        else:
            raw = getattr(seg, "text", "").strip()
        if not raw:
            continue
        seg_sentences = len(_SENTENCE_END.findall(raw)) + 1  # +1 for the last sentence

        # Flush paragraph if we'd exceed limits
        if current_parts and (
            current_chars + len(raw) > target_chars
            or current_sentences + seg_sentences > max_sentences
        ):
            paragraphs.append(" ".join(current_parts))
            current_parts = []
            current_chars = 0
            current_sentences = 0

        current_parts.append(raw)
        current_chars += len(raw)
        current_sentences += seg_sentences

    if current_parts:
        paragraphs.append(" ".join(current_parts))

    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# Transcript writing
# ---------------------------------------------------------------------------

def _yaml_str(value: str) -> str:
    """Return a YAML-safe quoted string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_transcript_markdown(
    video_path: Path,
    text: str,
    segments: list,
    metadata: dict,
    model: str,
    language: str,
    backend: str,
) -> str:
    """Assemble the full transcript.md content."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Duration from last segment end time (fallback to 0)
    duration_sec = 0
    if segments:
        last = segments[-1]
        if isinstance(last, dict):
            duration_sec = int(last.get("end", 0))
        else:
            duration_sec = int(getattr(last, "end", 0))

    word_count = len(text.split()) if text else 0
    lesson_title = metadata.get("lesson_title", video_path.parent.name)
    module_title = metadata.get("module_title", "")
    lesson_id = metadata.get("lesson_id", "")
    relative_path = metadata.get("relative_path", "")

    frontmatter_lines = [
        "---",
        f"source: {video_path.name}",
        f"lesson_title: {_yaml_str(lesson_title)}",
        f"module_title: {_yaml_str(module_title)}",
        f"lesson_id: {_yaml_str(lesson_id)}",
        f"relative_path: {_yaml_str(relative_path)}",
        f"transcribed_at: {_yaml_str(now_iso)}",
        f"model: {_yaml_str(model)}",
        f"transcription_backend: {_yaml_str(backend)}",
        f"language: {_yaml_str(language)}",
        f"duration_sec: {duration_sec}",
        f"word_count: {word_count}",
        "---",
    ]
    frontmatter = "\n".join(frontmatter_lines)

    body = build_paragraphs(segments)
    if not body:
        # Fallback: whole text as one paragraph (shouldn't happen in practice)
        body = text.strip()

    return f"{frontmatter}\n\n# {lesson_title}\n\n{body}\n"


def write_atomic(path: Path, content: str) -> None:
    """Write content to a .tmp file then atomically replace the target."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up .tmp on any error (including KeyboardInterrupt)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_failed_sentinel(path: Path, error_msg: str) -> None:
    """Write a .FAILED sentinel file with the error message."""
    failed_path = path.with_suffix(path.suffix + ".FAILED")
    try:
        content = f"# Transcription failed\n\n{error_msg}\n"
        write_atomic(failed_path, content)
    except OSError as exc:
        print(f"  WARNING: could not write FAILED sentinel: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Video discovery
# ---------------------------------------------------------------------------

def find_videos(root: Path, filename: str) -> list[Path]:
    """
    Walk root recursively and return all paths matching filename,
    sorted by their string representation for deterministic ordering.
    """
    found = []
    for dirpath, _dirs, files in os.walk(root):
        if filename in files:
            found.append(Path(dirpath) / filename)
    found.sort(key=lambda p: str(p))
    return found


# ---------------------------------------------------------------------------
# Prefetch worker (pipeline: warm OS cache while GPU transcribes current)
# ---------------------------------------------------------------------------

def _noop_prefetch(video_path: Path) -> None:
    """
    Touch the video file to warm the OS file cache for the next call.
    A simple readahead is enough to pipeline I/O with GPU work.
    """
    try:
        with open(video_path, "rb") as fh:
            # Read first 4 MB to warm page cache; the rest streams naturally
            fh.read(4 * 1024 * 1024)
    except OSError:
        pass  # Non-fatal — it's just a cache hint


# ---------------------------------------------------------------------------
# Core transcription — parakeet backend
# ---------------------------------------------------------------------------

# Module-level model cache: keyed by (hf_id, use_local_attn)
# Avoids reloading the model for every video in a batch run.
_parakeet_model_cache: dict = {}

# Threshold (seconds) above which local attention is enabled to avoid OOM
# on 16-32 GB Macs. Parakeet full-attention caps around 24 min; use 20 min
# as the safety margin.
_LOCAL_ATTN_THRESHOLD_SEC = 1200  # 20 minutes


def _get_parakeet_model(from_pretrained_fn, model_id: str, use_local_attn: bool):
    """
    Load (or retrieve cached) Parakeet model, toggling local attention as needed.

    Local attention and full attention require different pos_enc state in the
    Conformer, so we cache them separately rather than toggling in place.
    """
    cache_key = (model_id, use_local_attn)
    if cache_key in _parakeet_model_cache:
        return _parakeet_model_cache[cache_key]

    print(
        f"  Loading model {model_id}"
        f" ({'local attention' if use_local_attn else 'full attention'})…",
        flush=True,
    )
    model = from_pretrained_fn(model_id)
    if use_local_attn:
        model.encoder.set_attention_model("rel_pos_local_attn", (256, 256))

    _parakeet_model_cache[cache_key] = model
    return model


def transcribe_one_parakeet(
    video_path: Path,
    output_name: str,
    model_id: str,
    language: Optional[str],
    from_pretrained_fn,
    index: int,
    total: int,
) -> tuple[bool, str]:
    """
    Transcribe a single video with parakeet-mlx.
    Returns (success, message).
    On failure, writes a .FAILED sentinel and returns (False, error_msg).

    Language note: Parakeet-TDT is an English-only model. The --language flag
    is accepted but ignored; it exists only for CLI shape compatibility.
    """
    import time  # noqa: PLC0415

    transcript_path = video_path.parent / output_name
    wall_start = time.monotonic()

    # Probe audio duration to decide attention model
    duration_probe = probe_duration(video_path)
    use_local_attn = duration_probe is not None and duration_probe > _LOCAL_ATTN_THRESHOLD_SEC

    if use_local_attn:
        print(
            f"  Long audio detected ({duration_probe:.0f}s > {_LOCAL_ATTN_THRESHOLD_SEC}s) "
            f"— enabling local attention to reduce memory usage.",
            flush=True,
        )

    try:
        model = _get_parakeet_model(from_pretrained_fn, model_id, use_local_attn)

        # For very long audio (>20 min), use chunked transcription to cap
        # peak memory. chunk_duration=120s with 15s overlap is the library default.
        chunk_kwargs: dict = {}
        if use_local_attn:
            chunk_kwargs["chunk_duration"] = 120.0
            chunk_kwargs["overlap_duration"] = 15.0

        result = model.transcribe(str(video_path), **chunk_kwargs)

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        write_failed_sentinel(transcript_path, error_msg)
        return False, f"FAILED: {type(exc).__name__}: {exc}"

    wall_elapsed = time.monotonic() - wall_start

    # AlignedResult has .text (str) and .sentences (list[AlignedSentence])
    text = result.text.strip()
    sentences = result.sentences  # list of AlignedSentence objects

    # Duration from last sentence end
    duration_sec = int(sentences[-1].end) if sentences else 0
    word_count = len(text.split()) if text else 0

    # Parakeet is English-only; report as "en" unless the user overrides
    detected_lang = language if language else "en"

    metadata = read_lesson_metadata(video_path.parent)

    content = build_transcript_markdown(
        video_path=video_path,
        text=text,
        segments=sentences,
        metadata=metadata,
        model=model_id,
        language=detected_lang,
        backend="local-parakeet-mlx",
    )

    write_atomic(transcript_path, content)

    rel = video_path.relative_to(video_path.parents[2]) if len(video_path.parts) >= 3 else video_path
    rtf = (duration_sec / wall_elapsed) if wall_elapsed > 0 else 0
    msg = (
        f"[{index}/{total}] {rel} → {output_name} "
        f"({duration_sec}s audio, {wall_elapsed:.0f}s wall, {rtf:.0f}× RT, {word_count} words)"
    )
    return True, msg


# ---------------------------------------------------------------------------
# Core transcription — whisper fallback backend
# ---------------------------------------------------------------------------

def transcribe_one_whisper(
    video_path: Path,
    output_name: str,
    model: str,
    language: Optional[str],
    mlx_whisper,
    index: int,
    total: int,
) -> tuple[bool, str]:
    """
    Transcribe a single video with mlx-whisper (fallback backend).
    Returns (success, message).
    On failure, writes a .FAILED sentinel and returns (False, error_msg).
    """
    import time  # noqa: PLC0415

    transcript_path = video_path.parent / output_name

    extra_kwargs = {}
    if language:
        extra_kwargs["language"] = language

    wall_start = time.monotonic()

    try:
        result = mlx_whisper.transcribe(
            str(video_path),
            path_or_hf_repo=model,
            **extra_kwargs,
        )
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        write_failed_sentinel(transcript_path, error_msg)
        return False, f"FAILED: {type(exc).__name__}: {exc}"

    wall_elapsed = time.monotonic() - wall_start

    if not isinstance(result, dict):
        error_msg = f"mlx_whisper returned {type(result).__name__}, expected dict"
        write_failed_sentinel(transcript_path, error_msg)
        return False, f"FAILED: {error_msg}"

    text = result.get("text", "").strip()
    segments = result.get("segments", [])
    detected_lang = result.get("language", "unknown")

    duration_sec = int(segments[-1].get("end", 0)) if segments else 0
    word_count = len(text.split()) if text else 0

    metadata = read_lesson_metadata(video_path.parent)

    content = build_transcript_markdown(
        video_path=video_path,
        text=text,
        segments=segments,
        metadata=metadata,
        model=model,
        language=detected_lang,
        backend="local-mlx-whisper",
    )

    write_atomic(transcript_path, content)

    rel = video_path.relative_to(video_path.parents[2]) if len(video_path.parts) >= 3 else video_path
    rtf = (duration_sec / wall_elapsed) if wall_elapsed > 0 else 0
    msg = (
        f"[{index}/{total}] {rel} → {output_name} "
        f"({duration_sec}s audio, {wall_elapsed:.0f}s wall, {rtf:.0f}× RT, {word_count} words)"
    )
    return True, msg


# ---------------------------------------------------------------------------
# Unified dispatch
# ---------------------------------------------------------------------------

def transcribe_one(
    video_path: Path,
    output_name: str,
    model: str,
    language: Optional[str],
    backend: str,
    # backend-specific handles (one will be None)
    from_pretrained_fn,
    mlx_whisper,
    index: int,
    total: int,
) -> tuple[bool, str]:
    """Dispatch to the appropriate backend."""
    if backend == "parakeet":
        return transcribe_one_parakeet(
            video_path=video_path,
            output_name=output_name,
            model_id=model,
            language=language,
            from_pretrained_fn=from_pretrained_fn,
            index=index,
            total=total,
        )
    else:
        return transcribe_one_whisper(
            video_path=video_path,
            output_name=output_name,
            model=model,
            language=language,
            mlx_whisper=mlx_whisper,
            index=index,
            total=total,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Transcribe videos in a directory tree using parakeet-mlx "
            "(default) or mlx-whisper (--backend whisper)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("root", help="Root directory to walk for video files.")
    p.add_argument(
        "--backend",
        choices=["parakeet", "whisper"],
        default="parakeet",
        help=(
            "Transcription backend. 'parakeet' (default): NVIDIA Parakeet-TDT-0.6B-v3 "
            "via parakeet-mlx — ~60× real-time, English-only, CTC/TDT decoder (no repetition "
            "loops). 'whisper': mlx-whisper fallback — multilingual but prone to hallucination "
            "loops on silence."
        ),
    )
    p.add_argument(
        "--model",
        default="mlx-community/parakeet-tdt-0.6b-v3",
        help=(
            "Model identifier. For parakeet backend: HuggingFace repo ID "
            "(default: mlx-community/parakeet-tdt-0.6b-v3). "
            "For whisper backend: mlx-whisper model path/repo "
            "(e.g. mlx-community/whisper-large-v3-mlx)."
        ),
    )
    p.add_argument(
        "--filename",
        default="video.mp4",
        help="Video filename to match (default: video.mp4)",
    )
    p.add_argument(
        "--output",
        default="transcript.md",
        help="Transcript filename to write (default: transcript.md)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-transcribe even if transcript already exists.",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip videos that already have a transcript (same as default; explicit alias).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N transcriptions (useful for smoke tests).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be transcribed without calling the model.",
    )
    p.add_argument(
        "--language",
        default=None,
        metavar="CODE",
        help=(
            "Language code hint (e.g. 'en'). For the parakeet backend this is recorded "
            "in the frontmatter but ignored by the model (Parakeet-TDT is English-only). "
            "For the whisper backend this is passed to mlx-whisper for forced language decoding."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()

    if not root.exists():
        print(f"ERROR: root directory does not exist: {root}", file=sys.stderr)
        return 1

    # Dependency checks
    check_ffmpeg()

    from_pretrained_fn = None
    mlx_whisper = None

    if not args.dry_run:
        if args.backend == "parakeet":
            from_pretrained_fn = import_parakeet()
        else:
            mlx_whisper = import_mlx_whisper()

    # Discover videos
    videos = find_videos(root, args.filename)
    total_found = len(videos)

    if total_found == 0:
        print(f"No '{args.filename}' files found under {root}")
        return 0

    # Apply --limit to the full ordered list BEFORE splitting into skip/pending.
    # This means "--limit 2" visits only the first 2 videos in sort order,
    # whether they are already done or not, matching intuitive smoke-test use.
    if args.limit is not None:
        videos_to_consider = videos[: args.limit]
    else:
        videos_to_consider = videos

    print(
        f"Found {total_found} video(s) under {root} "
        f"[backend={args.backend}, model={args.model}]"
    )

    # Separate videos into to-do vs already-done
    pending: list[Path] = []
    skipped = 0
    for vp in videos_to_consider:
        transcript_path = vp.parent / args.output
        if transcript_path.exists() and not args.force:
            skipped += 1
        else:
            pending.append(vp)

    if skipped:
        print(f"⏭️  {skipped} already transcribed (use --force to re-run)")

    total_pending = len(pending)

    if total_pending == 0:
        print("Nothing to do.")
        return 0

    # Dry-run: just list
    if args.dry_run:
        print(f"\nDry-run: would transcribe {total_pending} video(s):")
        for i, vp in enumerate(pending, 1):
            try:
                rel = vp.relative_to(root)
            except ValueError:
                rel = vp
            print(f"  [{i}/{total_pending}] {rel}")
        return 0

    # Actual transcription with pipelined prefetch
    done = 0
    failed = 0
    # Use the considered set size as the denominator so progress indices are
    # consistent whether or not --limit was applied.
    global_total = len(videos_to_consider)

    with ThreadPoolExecutor(max_workers=1) as prefetch_pool:
        prefetch_future: Optional[Future] = None

        # Kick off prefetch for the first video
        if pending:
            prefetch_future = prefetch_pool.submit(_noop_prefetch, pending[0])

        for idx, video_path in enumerate(pending):
            if _shutdown_requested:
                print(f"\n🛑 Shutting down after {done} transcription(s).")
                break

            # Wait for the current prefetch to finish (it was for this video)
            if prefetch_future is not None:
                try:
                    prefetch_future.result(timeout=30)
                except Exception:
                    pass  # Prefetch failure is non-fatal

            # Kick off prefetch for the NEXT video
            next_idx = idx + 1
            if next_idx < len(pending):
                prefetch_future = prefetch_pool.submit(_noop_prefetch, pending[next_idx])
            else:
                prefetch_future = None

            display_index = skipped + done + failed + 1

            # Idempotency check (re-check in case a parallel process wrote it)
            transcript_path = video_path.parent / args.output
            if transcript_path.exists() and not args.force:
                try:
                    rel = video_path.relative_to(root)
                except ValueError:
                    rel = video_path
                print(f"⏭️  [{display_index}/{global_total}] {rel} — already transcribed", flush=True)
                continue

            success, msg = transcribe_one(
                video_path=video_path,
                output_name=args.output,
                model=args.model,
                language=args.language,
                backend=args.backend,
                from_pretrained_fn=from_pretrained_fn,
                mlx_whisper=mlx_whisper,
                index=display_index,
                total=global_total,
            )

            if success:
                done += 1
                print(f"✅ {msg}", flush=True)
            else:
                failed += 1
                print(f"❌ [{display_index}/{global_total}] {video_path.name} — {msg}", flush=True)

    print(f"\nDone. ✅ {done} transcribed, ❌ {failed} failed, ⏭️  {skipped} skipped.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
