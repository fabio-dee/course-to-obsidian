#!/usr/bin/env python3
"""
Remote video transcription tool for skool-downloader.

Walks a directory tree, finds video files, and writes a transcript markdown
file next to each one by uploading to a faster-whisper HTTP server
(OpenAI-compatible /v1/audio/transcriptions endpoint).

Output filename is derived from `lesson.json.title` in the video's folder:
    title "2. Limiting beliefs"  ->  "2. Limiting beliefs.remote.md"
Override with --output <name> to force a fixed filename.

The `.remote.md` extension guarantees no collision with the local script's
`transcript.md` files, so this can run in parallel with transcribe_videos.py.

Parallelism: one worker per --servers URL, pulling from a shared thread-safe
queue. Resumable via a state file that records per-video attempt history and
distinguishes transient network failures from permanent video-level errors.

SETUP (one-time):
    python3 -m venv .venv-transcribe
    source .venv-transcribe/bin/activate
    pip install -r scripts/transcribe_videos_remote.requirements.txt
    # ffmpeg required on PATH only if --use-ffmpeg flag is passed

USAGE:
    python scripts/transcribe_videos_remote.py <root-dir> [options]
    python scripts/transcribe_videos_remote.py downloads/makerschool --limit 5
    python scripts/transcribe_videos_remote.py downloads/makerschool --dry-run
    python scripts/transcribe_videos_remote.py downloads/makerschool \\
        --servers http://192.168.8.165:8010,http://192.168.8.165:8011
    python scripts/transcribe_videos_remote.py downloads/makerschool --retry-failed
"""

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SERVERS = [
    "http://192.168.8.165:8000",
]

DEFAULT_MODEL = "nvidia/parakeet-tdt-0.6b-v3"
DEFAULT_FILENAME = "video.mp4"
DEFAULT_STATE_FILENAME = ".transcribe-remote-state.json"

# Retry/backoff for a single POST attempt.
POST_BACKOFF_SECONDS = [2, 8, 32]
POST_MAX_ATTEMPTS = 3
POST_TIMEOUT_SEC = 600

# How long to block the whole run when ALL servers are unhealthy before we
# start marking things in_progress and bailing.
GLOBAL_UNHEALTHY_GRACE_SEC = 120  # 2 min
UNHEALTHY_POLL_INTERVAL_SEC = 30

# ---------------------------------------------------------------------------
# Global state for graceful interrupt
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_sigint(signum, frame):  # noqa: ANN001
    global _shutdown_requested
    print(
        "\n\nWARNING: SIGINT received — will finish current upload(s) then exit cleanly.",
        flush=True,
    )
    _shutdown_requested = True


signal.signal(signal.SIGINT, _handle_sigint)


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def check_ffmpeg() -> bool:
    """Return True if ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


def import_requests():
    """Import requests or abort with clear instructions."""
    try:
        import requests  # noqa: PLC0415
        return requests
    except ImportError:
        sys.exit(
            "ERROR: requests is not installed.\n"
            "Run:\n"
            "  source .venv-transcribe/bin/activate\n"
            "  pip install -r scripts/transcribe_videos_remote.requirements.txt\n"
        )


# ---------------------------------------------------------------------------
# Server health tracking
# ---------------------------------------------------------------------------

class ServerPool:
    """
    Tracks health of each configured whisper server. Used by workers to
    decide whether their assigned server is healthy, and by the main loop
    to detect global outages.
    """

    def __init__(self, urls: list[str], requests_module):
        self._requests = requests_module
        self._lock = threading.Lock()
        # Normalize (strip trailing slash) and de-duplicate while keeping order.
        seen: set[str] = set()
        self.urls: list[str] = []
        for u in urls:
            nu = u.rstrip("/")
            if nu not in seen:
                seen.add(nu)
                self.urls.append(nu)
        self._healthy: dict[str, bool] = {u: False for u in self.urls}
        self._last_checked: dict[str, float] = {u: 0.0 for u in self.urls}

    def check(self, url: str, timeout: float = 5.0) -> bool:
        """Ping /health and update healthy flag. Return current healthy state."""
        health_url = f"{url}/health"
        healthy = False
        try:
            resp = self._requests.get(health_url, timeout=timeout)
            healthy = resp.status_code == 200
        except Exception:
            healthy = False
        with self._lock:
            self._healthy[url] = healthy
            self._last_checked[url] = time.monotonic()
        return healthy

    def check_all(self, timeout: float = 5.0) -> dict[str, bool]:
        """Health-check every URL serially; return {url: healthy}. Cheap enough."""
        result = {}
        for u in self.urls:
            result[u] = self.check(u, timeout=timeout)
        return result

    def is_healthy(self, url: str) -> bool:
        with self._lock:
            return self._healthy.get(url, False)

    def healthy_urls(self) -> list[str]:
        with self._lock:
            return [u for u, h in self._healthy.items() if h]

    def any_healthy(self) -> bool:
        return bool(self.healthy_urls())


def server_host_port(url: str) -> tuple[str, int]:
    """Return (host, port) parsed from a URL. Default port 80/443 if missing."""
    stripped = url.replace("http://", "").replace("https://", "").rstrip("/")
    # Drop any path
    stripped = stripped.split("/", 1)[0]
    if ":" in stripped:
        host, port_str = stripped.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 80
        return host, port
    return stripped, 443 if url.startswith("https") else 80


# ---------------------------------------------------------------------------
# Filename derivation from lesson.json.title
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')
_WHITESPACE_RUN = re.compile(r"\s+")
_MAX_STEM_LEN = 200  # leave room for ".remote.md" + ".FAILED"


def sanitize_title_for_filename(title: str) -> str:
    """
    Turn a lesson.json title into a filesystem-safe stem.

    - Replace / \\ : * ? " < > | with '-'
    - Collapse whitespace runs to a single space
    - Strip leading/trailing dots and whitespace
    - Cap at 200 chars
    - If empty after sanitization, return "untitled"
    """
    s = _UNSAFE_CHARS.sub("-", title)
    s = _WHITESPACE_RUN.sub(" ", s)
    s = s.strip(" .\t\n\r")
    if len(s) > _MAX_STEM_LEN:
        s = s[:_MAX_STEM_LEN].rstrip(" .")
    if not s:
        return "untitled"
    return s


def _strip_index_prefix(name: str) -> str:
    """Remove leading 'N-' prefix from a folder name."""
    return re.sub(r"^\d+-", "", name).strip()


def read_lesson_metadata(lesson_dir: Path) -> dict:
    """
    Read lesson.json from lesson_dir, return a dict of frontmatter fields
    plus a 'title' field used for filename derivation.
    """
    json_path = lesson_dir / "lesson.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            title = data.get("title") or _strip_index_prefix(lesson_dir.name)
            return {
                "title": title,
                "lesson_title": data.get("title", _strip_index_prefix(lesson_dir.name)),
                "module_title": data.get("moduleTitle", ""),
                "lesson_id": data.get("lessonId", ""),
                "relative_path": data.get("relativePath", ""),
            }
        except (json.JSONDecodeError, OSError):
            pass
    fallback = _strip_index_prefix(lesson_dir.name) or lesson_dir.name
    return {
        "title": fallback,
        "lesson_title": fallback,
        "module_title": "",
        "lesson_id": "",
        "relative_path": "",
    }


def derive_output_filename(video_path: Path, override: Optional[str]) -> str:
    """
    Compute the output markdown filename for a given video.

    If --output override is set, always use it (legacy behavior).
    Otherwise: sanitize(lesson.json.title) + ".remote.md", falling back to
    stripped folder name if lesson.json is missing.
    """
    if override:
        return override
    meta = read_lesson_metadata(video_path.parent)
    stem = sanitize_title_for_filename(meta["title"])
    return f"{stem}.remote.md"


# ---------------------------------------------------------------------------
# Paragraph grouping (identical to local script)
# ---------------------------------------------------------------------------

def build_paragraphs(segments: list, target_chars: int = 600, max_sentences: int = 5) -> str:
    """Group Whisper segments into human-readable paragraphs."""
    if not segments:
        return ""

    paragraphs: list[str] = []
    current_parts: list[str] = []
    current_chars = 0
    current_sentences = 0

    _SENTENCE_END = re.compile(r"[.!?]\s")

    for seg in segments:
        raw = seg.get("text", "").strip()
        if not raw:
            continue
        seg_sentences = len(_SENTENCE_END.findall(raw)) + 1

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
    server: str,
    upload_sec: float,
    wall_sec: float,
) -> str:
    """Assemble the full transcript markdown content."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    duration_sec = 0
    if segments:
        last = segments[-1]
        duration_sec = int(last.get("end", 0))

    word_count = len(text.split()) if text else 0
    lesson_title = metadata.get("lesson_title", video_path.parent.name)
    module_title = metadata.get("module_title", "")
    lesson_id = metadata.get("lesson_id", "")
    relative_path = metadata.get("relative_path", "")

    server_host, server_port = server_host_port(server)

    frontmatter_lines = [
        "---",
        f"source: {video_path.name}",
        f"lesson_title: {_yaml_str(lesson_title)}",
        f"module_title: {_yaml_str(module_title)}",
        f"lesson_id: {_yaml_str(lesson_id)}",
        f"relative_path: {_yaml_str(relative_path)}",
        f"transcribed_at: {_yaml_str(now_iso)}",
        f"model: {_yaml_str(model)}",
        'transcription_backend: "remote-faster-whisper"',
        f"server: {_yaml_str(f'{server_host}:{server_port}')}",
        f"server_port: {server_port}",
        f"language: {_yaml_str(language)}",
        f"duration_sec: {duration_sec}",
        f"word_count: {word_count}",
        f"upload_sec: {upload_sec:.1f}",
        f"inference_sec: {wall_sec:.1f}",
        "---",
    ]
    frontmatter = "\n".join(frontmatter_lines)

    body = build_paragraphs(segments)
    if not body:
        body = text.strip()

    return f"{frontmatter}\n\n# {lesson_title}\n\n{body}\n"


def write_atomic(path: Path, content: str) -> None:
    """Write content to a .tmp file then atomically replace the target."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_failed_sentinel(path: Path, error_msg: str) -> None:
    """Write a .FAILED sentinel file with the error message."""
    failed_path = Path(str(path) + ".FAILED")
    try:
        content = f"# Transcription failed\n\n{error_msg}\n"
        write_atomic(failed_path, content)
    except OSError as exc:
        print(f"  WARNING: could not write FAILED sentinel: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Video discovery
# ---------------------------------------------------------------------------

def find_videos(root: Path, filename: str) -> list[Path]:
    """Walk root recursively for files named filename. Deterministic sort."""
    found = []
    for dirpath, _dirs, files in os.walk(root):
        if filename in files:
            found.append(Path(dirpath) / filename)
    found.sort(key=lambda p: str(p))
    return found


# ---------------------------------------------------------------------------
# ffmpeg fallback
# ---------------------------------------------------------------------------

def extract_audio_to_temp(video_path: Path) -> Path:
    """Extract audio to a temp m4a. Caller deletes."""
    tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "aac", "-b:a", "64k",
            str(tmp_path),
            "-loglevel", "error",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}) for {video_path}: {result.stderr[:300]}"
        )
    return tmp_path


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class TransientError(Exception):
    """Network / server-side error that should be retried on the next run."""


class PermanentError(Exception):
    """Video-level error that won't be fixed by retrying (e.g. bad format)."""


def classify_http_error(status_code: int, body_snippet: str) -> Exception:
    """
    Decide whether an HTTP error is transient (retry next run) or permanent
    (write .FAILED sentinel, don't bother again).

    4xx = permanent (caller sent something bad)
    5xx / connection / timeout = transient
    """
    msg = f"HTTP {status_code}: {body_snippet}"
    if 400 <= status_code < 500:
        return PermanentError(msg)
    return TransientError(msg)


# ---------------------------------------------------------------------------
# Remote POST with retry
# ---------------------------------------------------------------------------

def _post_transcription(
    audio_path: Path,
    mime_type: str,
    server: str,
    model: str,
    language: Optional[str],
    requests,
) -> dict:
    """
    POST audio to the remote server with retry+backoff.
    Raises TransientError or PermanentError on final failure.
    """
    url = f"{server.rstrip('/')}/v1/audio/transcriptions"
    last_exc: Optional[Exception] = None

    for attempt_num in range(1, POST_MAX_ATTEMPTS + 1):
        try:
            with open(audio_path, "rb") as fh:
                files = {"file": (audio_path.name, fh, mime_type)}
                data: dict = {
                    "model": model,
                    "response_format": "verbose_json",
                }
                if language:
                    data["language"] = language

                resp = requests.post(url, files=files, data=data, timeout=POST_TIMEOUT_SEC)

            if resp.status_code == 200:
                return resp.json()

            body_snippet = resp.text[:500]
            err = classify_http_error(resp.status_code, body_snippet)
            if isinstance(err, PermanentError):
                # Don't retry 4xx — it won't get better.
                raise err
            last_exc = err
            if attempt_num < POST_MAX_ATTEMPTS:
                wait = POST_BACKOFF_SECONDS[attempt_num - 1]
                print(
                    f"  [{server}] attempt {attempt_num}/{POST_MAX_ATTEMPTS} failed: {err}. "
                    f"Retrying in {wait}s...",
                    flush=True,
                )
                time.sleep(wait)

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = TransientError(f"{type(exc).__name__}: {exc}")
            if attempt_num < POST_MAX_ATTEMPTS:
                wait = POST_BACKOFF_SECONDS[attempt_num - 1]
                print(
                    f"  [{server}] attempt {attempt_num}/{POST_MAX_ATTEMPTS} "
                    f"network error: {exc}. Retrying in {wait}s...",
                    flush=True,
                )
                time.sleep(wait)

    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# State file (resumable progress)
# ---------------------------------------------------------------------------

class StateStore:
    """
    Persistent record of per-video attempts. Serialized to JSON on disk with
    atomic writes. Thread-safe.

    Key is the video path relative to the root dir.
    """

    VERSION = 1

    def __init__(self, path: Path, root: Path):
        self.path = path
        self.root = root
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if d.get("version") != self.VERSION:
                    print(
                        f"WARNING: state file version mismatch ({d.get('version')} "
                        f"!= {self.VERSION}); starting fresh.",
                        flush=True,
                    )
                    return self._blank()
                d.setdefault("videos", {})
                return d
            except (json.JSONDecodeError, OSError) as exc:
                print(
                    f"WARNING: could not read state file {self.path}: {exc}. Starting fresh.",
                    flush=True,
                )
        return self._blank()

    def _blank(self) -> dict:
        return {
            "version": self.VERSION,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "videos": {},
        }

    def _save_locked(self) -> None:
        """Caller must hold self._lock."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def key_for(self, video_path: Path) -> str:
        try:
            return str(video_path.relative_to(self.root))
        except ValueError:
            return str(video_path)

    def get(self, video_path: Path) -> dict:
        key = self.key_for(video_path)
        with self._lock:
            return dict(self._data["videos"].get(key, {}))

    def record(
        self,
        video_path: Path,
        *,
        status: str,
        server_used: Optional[str] = None,
        output_file: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        """Update the per-video entry and persist."""
        key = self.key_for(video_path)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            entry = self._data["videos"].get(key, {"attempts": 0})
            entry["status"] = status
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["last_attempt_at"] = now_iso
            if server_used is not None:
                entry["server_used"] = server_used
            if output_file is not None:
                entry["output_file"] = output_file
            if last_error is not None:
                entry["last_error"] = last_error
            elif status == "completed":
                # Clear stale error on success.
                entry.pop("last_error", None)
            self._data["videos"][key] = entry
            self._save_locked()

    def mark_pending(self, video_path: Path, output_file: str) -> None:
        """Reset an entry to pending (e.g. on --retry-failed)."""
        key = self.key_for(video_path)
        with self._lock:
            entry = self._data["videos"].get(key, {"attempts": 0})
            entry["status"] = "pending"
            entry["output_file"] = output_file
            entry.pop("last_error", None)
            self._data["videos"][key] = entry
            self._save_locked()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))  # deep copy


# ---------------------------------------------------------------------------
# Worker: one per server URL
# ---------------------------------------------------------------------------

def transcribe_one(
    video_path: Path,
    output_name: str,
    server: str,
    model: str,
    language: Optional[str],
    requests,
    use_ffmpeg: bool,
) -> tuple[bool, str, dict]:
    """
    Transcribe one video via `server`.
    Returns (success, message, stats_dict). stats contains:
        - duration_sec, wall_sec, upload_sec, word_count, error_kind
    """
    transcript_path = video_path.parent / output_name
    tmp_audio_path: Optional[Path] = None

    wall_start = time.monotonic()
    stats: dict = {
        "duration_sec": 0,
        "wall_sec": 0.0,
        "upload_sec": 0.0,
        "word_count": 0,
        "error_kind": None,
    }

    try:
        if use_ffmpeg:
            audio_path = extract_audio_to_temp(video_path)
            tmp_audio_path = audio_path
            mime_type = "audio/m4a"
        else:
            audio_path = video_path
            mime_type = "video/mp4"

        upload_start = time.monotonic()

        try:
            result = _post_transcription(
                audio_path=audio_path,
                mime_type=mime_type,
                server=server,
                model=model,
                language=language,
                requests=requests,
            )
        except PermanentError as exc:
            error_msg = f"PermanentError: {exc}\n\n{traceback.format_exc()}"
            write_failed_sentinel(transcript_path, error_msg)
            stats["error_kind"] = "permanent"
            stats["wall_sec"] = time.monotonic() - wall_start
            return False, f"PERMANENT: {exc}", stats
        except TransientError as exc:
            stats["error_kind"] = "transient"
            stats["wall_sec"] = time.monotonic() - wall_start
            return False, f"TRANSIENT: {exc}", stats
        except Exception as exc:
            # Unknown error class: treat as transient (safe side) but log trace.
            traceback.print_exc()
            stats["error_kind"] = "transient"
            stats["wall_sec"] = time.monotonic() - wall_start
            return False, f"TRANSIENT: unknown: {type(exc).__name__}: {exc}", stats

        stats["upload_sec"] = time.monotonic() - upload_start

    finally:
        if tmp_audio_path is not None:
            try:
                tmp_audio_path.unlink(missing_ok=True)
            except OSError:
                pass

    stats["wall_sec"] = time.monotonic() - wall_start

    if not isinstance(result, dict):
        error_msg = f"Server returned {type(result).__name__}, expected dict"
        write_failed_sentinel(transcript_path, error_msg)
        stats["error_kind"] = "permanent"
        return False, f"PERMANENT: {error_msg}", stats

    text = result.get("text", "").strip()
    segments = result.get("segments", [])
    detected_lang = result.get("language", language or "unknown")

    duration_float = result.get("duration", 0)
    if not duration_float and segments:
        duration_float = segments[-1].get("end", 0)
    stats["duration_sec"] = int(duration_float)
    stats["word_count"] = len(text.split()) if text else 0

    metadata = read_lesson_metadata(video_path.parent)

    content = build_transcript_markdown(
        video_path=video_path,
        text=text,
        segments=segments,
        metadata=metadata,
        model=model,
        language=detected_lang,
        server=server,
        upload_sec=stats["upload_sec"],
        wall_sec=stats["wall_sec"],
    )

    write_atomic(transcript_path, content)

    msg = (
        f"{stats['duration_sec']}s audio, "
        f"{stats['wall_sec']:.0f}s wall, "
        f"{stats['upload_sec']:.0f}s upload+infer, "
        f"{stats['word_count']} words"
    )
    return True, msg, stats


# ---------------------------------------------------------------------------
# Worker loop: one thread per server
# ---------------------------------------------------------------------------

def worker_loop(
    *,
    worker_id: int,
    server: str,
    task_queue: "queue.Queue",
    state: StateStore,
    pool: ServerPool,
    root: Path,
    global_total: int,
    output_override: Optional[str],
    model: str,
    language: Optional[str],
    requests,
    use_ffmpeg: bool,
    force: bool,
    counters: dict,
    counters_lock: threading.Lock,
    progress_lock: threading.Lock,
) -> None:
    """
    Pull videos off the queue. For each: check sibling idempotency, POST to
    this worker's server, record result in state file.
    """
    _host, port = server_host_port(server)
    tag = f":{port}"

    while not _shutdown_requested:
        try:
            video_path = task_queue.get(timeout=1.0)
        except queue.Empty:
            if task_queue.unfinished_tasks == 0:
                break
            continue
        if video_path is None:  # sentinel
            task_queue.task_done()
            break

        try:
            output_name = derive_output_filename(video_path, output_override)
            transcript_path = video_path.parent / output_name

            # Re-check idempotency (another worker or prior run may have written).
            if transcript_path.exists() and not force:
                with counters_lock:
                    counters["skipped"] += 1
                    processed = counters["skipped"] + counters["completed"] + counters["failed_perm"] + counters["failed_trans"]
                state.record(
                    video_path,
                    status="completed",
                    output_file=output_name,
                    server_used=None,
                )
                try:
                    rel = video_path.relative_to(root)
                except ValueError:
                    rel = video_path
                with progress_lock:
                    print(
                        f"SKIP [{processed}/{global_total}] [{tag}] {rel} -> {output_name} (already exists)",
                        flush=True,
                    )
                continue

            # Wait for server health if down.
            if not pool.is_healthy(server):
                wait_for_health(server, pool, progress_lock, tag)
                if _shutdown_requested:
                    break

            state.record(
                video_path,
                status="in_progress",
                output_file=output_name,
                server_used=server_host_port(server)[0] + ":" + str(server_host_port(server)[1]),
            )

            success, msg, stats = transcribe_one(
                video_path=video_path,
                output_name=output_name,
                server=server,
                model=model,
                language=language,
                requests=requests,
                use_ffmpeg=use_ffmpeg,
            )

            try:
                rel = video_path.relative_to(root)
            except ValueError:
                rel = video_path

            if success:
                with counters_lock:
                    counters["completed"] += 1
                    processed = counters["skipped"] + counters["completed"] + counters["failed_perm"] + counters["failed_trans"]
                    counters["audio_sec_total"] += stats["duration_sec"]
                    counters["wall_sec_total"] += stats["wall_sec"]
                state.record(
                    video_path,
                    status="completed",
                    output_file=output_name,
                    server_used=f"{server_host_port(server)[0]}:{server_host_port(server)[1]}",
                )
                with progress_lock:
                    print(
                        f"OK [{processed}/{global_total}] [{tag}] {rel} -> {output_name} ({msg})",
                        flush=True,
                    )
            else:
                kind = stats.get("error_kind", "transient")
                if kind == "permanent":
                    with counters_lock:
                        counters["failed_perm"] += 1
                        processed = counters["skipped"] + counters["completed"] + counters["failed_perm"] + counters["failed_trans"]
                    state.record(
                        video_path,
                        status="failed",
                        last_error=msg,
                        output_file=output_name,
                        server_used=f"{server_host_port(server)[0]}:{server_host_port(server)[1]}",
                    )
                    with progress_lock:
                        print(
                            f"FAILED [{processed}/{global_total}] [{tag}] {rel} -> {output_name} — {msg}",
                            flush=True,
                        )
                else:
                    # Transient: mark server unhealthy and requeue for a healthy worker.
                    pool.check(server)  # refresh health state
                    with counters_lock:
                        counters["failed_trans"] += 1
                    state.record(
                        video_path,
                        status="in_progress",
                        last_error=msg,
                        output_file=output_name,
                        server_used=f"{server_host_port(server)[0]}:{server_host_port(server)[1]}",
                    )
                    with progress_lock:
                        print(
                            f"RETRY-LATER [{tag}] {rel} -> {output_name} — {msg} (requeuing)",
                            flush=True,
                        )
                    # Requeue only if at least one other server is healthy; else drop
                    # so the run terminates and can be resumed later.
                    if pool.any_healthy() and not _shutdown_requested:
                        task_queue.put(video_path)

        finally:
            task_queue.task_done()


def wait_for_health(
    server: str,
    pool: ServerPool,
    progress_lock: threading.Lock,
    tag: str,
) -> None:
    """
    Block until `server` reports healthy again, or until all servers have
    been unhealthy for GLOBAL_UNHEALTHY_GRACE_SEC, or until shutdown requested.
    """
    first_unhealthy = time.monotonic()
    announced = False
    while not _shutdown_requested:
        if pool.check(server):
            if announced:
                with progress_lock:
                    print(f"  [{tag}] server healthy again, resuming.", flush=True)
            return
        # If ANY server is healthy, this worker can stop blocking and let its
        # videos be handled by healthy peers (caller will requeue).
        if pool.any_healthy():
            if not announced:
                with progress_lock:
                    print(
                        f"  [{tag}] unhealthy; letting healthy peers take over.",
                        flush=True,
                    )
                announced = True
            # Brief pause then exit; worker will loop to next task.
            time.sleep(2)
            return
        # All servers down: wait with grace period.
        elapsed = time.monotonic() - first_unhealthy
        if not announced:
            with progress_lock:
                print(
                    f"  [{tag}] WAITING FOR SERVER — all servers unhealthy, polling every "
                    f"{UNHEALTHY_POLL_INTERVAL_SEC}s (grace {GLOBAL_UNHEALTHY_GRACE_SEC}s).",
                    flush=True,
                )
            announced = True
        if elapsed > GLOBAL_UNHEALTHY_GRACE_SEC:
            with progress_lock:
                print(
                    f"  [{tag}] grace period exceeded; will check again next loop.",
                    flush=True,
                )
            # Don't give up forever — just yield so shutdown_requested can be checked.
            time.sleep(UNHEALTHY_POLL_INTERVAL_SEC)
            return
        time.sleep(UNHEALTHY_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Transcribe videos via remote faster-whisper HTTP servers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("root", help="Root directory to walk for video files.")
    p.add_argument(
        "--servers",
        default=",".join(DEFAULT_SERVERS),
        help=(
            "Comma-separated list of server URLs. One worker per server. "
            f"Default: {','.join(DEFAULT_SERVERS)}"
        ),
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Max concurrent workers (default: len(healthy_servers)). "
            "Useful for dev/testing; normally leave unset."
        ),
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name on the remote servers (default: {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--filename",
        default=DEFAULT_FILENAME,
        help=f"Video filename to match (default: {DEFAULT_FILENAME})",
    )
    p.add_argument(
        "--output",
        default=None,
        help=(
            "Force a fixed output filename for every video. "
            "Default: derived from lesson.json.title (e.g. '1. Send 4 applications.remote.md')."
        ),
    )
    p.add_argument(
        "--state-file",
        default=None,
        help=(
            "Path to state file for resumable progress. "
            f"Default: <root>/{DEFAULT_STATE_FILENAME}"
        ),
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Re-attempt videos currently in 'failed' state (ignores .FAILED "
            "sentinel files for those videos)."
        ),
    )
    p.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete state file and start fresh. Requires --yes.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive operations like --reset-state.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-transcribe even if output markdown already exists.",
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
        help="List what would be transcribed without calling servers.",
    )
    p.add_argument(
        "--language",
        default=None,
        metavar="CODE",
        help="Force language hint (e.g. 'en'). Default: auto-detect.",
    )
    p.add_argument(
        "--use-ffmpeg",
        action="store_true",
        help="Extract audio to m4a before uploading (slower, smaller payload).",
    )
    p.add_argument(
        "--migrate-legacy",
        action="store_true",
        help=(
            "Rename legacy 'transcript.remote.md' (and '.FAILED' sentinels) to "
            "'<lesson-title>.remote.md' before starting. Non-destructive: skips "
            "if target already exists."
        ),
    )
    return p.parse_args()


LEGACY_FILENAME = "transcript.remote.md"


def migrate_legacy_filenames(
    root: Path,
    videos: list[Path],
    output_override: Optional[str],
) -> int:
    """
    Rename 'transcript.remote.md' -> '<title>.remote.md' (and sentinel) for each
    video folder that still has the legacy name. Returns count of files renamed.
    """
    if output_override:
        print("  --migrate-legacy skipped because --output override is set.", flush=True)
        return 0
    renamed = 0
    for vp in videos:
        folder = vp.parent
        legacy = folder / LEGACY_FILENAME
        legacy_failed = Path(str(legacy) + ".FAILED")
        new_name = derive_output_filename(vp, None)
        if new_name == LEGACY_FILENAME:
            continue
        new_path = folder / new_name
        new_failed = Path(str(new_path) + ".FAILED")
        if legacy.exists():
            if new_path.exists():
                print(f"  migrate-skip: both exist in {folder} — leaving legacy in place", flush=True)
            else:
                legacy.rename(new_path)
                renamed += 1
                print(f"  renamed: {legacy.name} -> {new_name}", flush=True)
        if legacy_failed.exists():
            if new_failed.exists():
                print(f"  migrate-skip: both sentinels exist in {folder}", flush=True)
            else:
                legacy_failed.rename(new_failed)
                renamed += 1
                print(f"  renamed: {legacy_failed.name} -> {new_failed.name}", flush=True)
    return renamed


def confirm_reset(state_path: Path, auto_yes: bool) -> bool:
    if not state_path.exists():
        return True
    if auto_yes:
        print(f"Deleting state file: {state_path}")
        state_path.unlink()
        return True
    print(
        f"ERROR: --reset-state requested but --yes not passed. Would delete: {state_path}",
        file=sys.stderr,
    )
    return False


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()

    if not root.exists():
        print(f"ERROR: root directory does not exist: {root}", file=sys.stderr)
        return 1

    requests = import_requests()

    use_ffmpeg = args.use_ffmpeg
    if use_ffmpeg and not check_ffmpeg():
        sys.exit(
            "ERROR: ffmpeg not found on PATH but --use-ffmpeg was requested.\n"
            "Install it with:  brew install ffmpeg"
        )

    # State file
    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else root / DEFAULT_STATE_FILENAME
    )
    if args.reset_state:
        if not confirm_reset(state_path, args.yes):
            return 1

    state = StateStore(state_path, root)

    # Server pool + health check
    server_urls = [s.strip() for s in args.servers.split(",") if s.strip()]
    if not server_urls:
        print("ERROR: no servers configured", file=sys.stderr)
        return 1

    pool = ServerPool(server_urls, requests)

    if not args.dry_run:
        print(f"Pre-flight health check on {len(pool.urls)} server(s)...", flush=True)
        health = pool.check_all(timeout=5.0)
        healthy = [u for u, h in health.items() if h]
        unhealthy = [u for u, h in health.items() if not h]
        for u in pool.urls:
            state_str = "OK" if health[u] else "DOWN"
            print(f"  {u}  [{state_str}]", flush=True)
        if not healthy:
            print("ERROR: no servers are reachable. Aborting.", file=sys.stderr)
            return 1
        if unhealthy:
            print(
                f"WARNING: {len(unhealthy)} server(s) down; running with "
                f"{len(healthy)} worker(s).",
                flush=True,
            )

    videos = find_videos(root, args.filename)
    total_found = len(videos)
    if total_found == 0:
        print(f"No '{args.filename}' files found under {root}")
        return 0

    if args.limit is not None:
        videos_to_consider = videos[: args.limit]
    else:
        videos_to_consider = videos

    print(f"Found {total_found} video(s) under {root}", flush=True)

    if args.migrate_legacy:
        print("Migrating legacy 'transcript.remote.md' filenames...", flush=True)
        renamed = migrate_legacy_filenames(root, videos_to_consider, args.output)
        print(f"  migrated {renamed} file(s).", flush=True)

    # Build the pending queue applying idempotency rules and state.
    pending: list[Path] = []
    skipped_existing = 0
    skipped_failed_sentinel = 0

    for vp in videos_to_consider:
        output_name = derive_output_filename(vp, args.output)
        transcript_path = vp.parent / output_name
        failed_sentinel = Path(str(transcript_path) + ".FAILED")

        # Force overrides all.
        if args.force:
            pending.append(vp)
            continue

        # Existing transcript sibling → skip.
        if transcript_path.exists():
            skipped_existing += 1
            # Mark as completed in state so the report is consistent.
            entry = state.get(vp)
            if entry.get("status") != "completed":
                state.record(
                    vp,
                    status="completed",
                    output_file=output_name,
                )
            continue

        # Prior permanent failure sentinel.
        if failed_sentinel.exists() and not args.retry_failed:
            skipped_failed_sentinel += 1
            continue

        # State-level failure (without sentinel, e.g. older runs).
        entry = state.get(vp)
        if entry.get("status") == "failed" and not args.retry_failed:
            skipped_failed_sentinel += 1
            continue

        if args.retry_failed and entry.get("status") == "failed":
            # Clear sentinel so classification gets a fresh try.
            if failed_sentinel.exists():
                try:
                    failed_sentinel.unlink()
                except OSError:
                    pass
            state.mark_pending(vp, output_name)

        pending.append(vp)

    if skipped_existing:
        print(f"  {skipped_existing} already transcribed (use --force to re-run)", flush=True)
    if skipped_failed_sentinel:
        print(
            f"  {skipped_failed_sentinel} marked permanently failed "
            f"(use --retry-failed to re-attempt)",
            flush=True,
        )

    total_pending = len(pending)
    if total_pending == 0:
        print("Nothing to do.")
        return 0

    if args.dry_run:
        print(f"\nDry-run: would transcribe {total_pending} video(s):", flush=True)
        for i, vp in enumerate(pending, 1):
            output_name = derive_output_filename(vp, args.output)
            try:
                rel = vp.relative_to(root)
            except ValueError:
                rel = vp
            print(f"  [{i}/{total_pending}] {rel} -> {output_name}")
        return 0

    healthy_urls = pool.healthy_urls()
    requested_concurrency = args.concurrency if args.concurrency else len(healthy_urls)
    concurrency = max(1, min(requested_concurrency, len(healthy_urls)))

    mode_label = "ffmpeg->m4a->upload" if use_ffmpeg else "direct-mp4-upload"
    print(
        f"Transcribing {total_pending} video(s) "
        f"[model={args.model}, mode={mode_label}, workers={concurrency}, "
        f"state={state_path.name}]",
        flush=True,
    )

    # Task queue + counters
    task_queue: queue.Queue = queue.Queue()
    for vp in pending:
        task_queue.put(vp)

    global_total = total_pending + skipped_existing
    counters = {
        "completed": 0,
        "skipped": skipped_existing,
        "failed_perm": 0,
        "failed_trans": 0,
        "audio_sec_total": 0,
        "wall_sec_total": 0.0,
    }
    counters_lock = threading.Lock()
    progress_lock = threading.Lock()

    # Assign one worker per healthy server (up to concurrency cap).
    worker_servers = healthy_urls[:concurrency]
    run_wall_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=concurrency) as pool_exec:
        futures = []
        for i, server in enumerate(worker_servers):
            fut = pool_exec.submit(
                worker_loop,
                worker_id=i,
                server=server,
                task_queue=task_queue,
                state=state,
                pool=pool,
                root=root,
                global_total=global_total,
                output_override=args.output,
                model=args.model,
                language=args.language,
                requests=requests,
                use_ffmpeg=use_ffmpeg,
                force=args.force,
                counters=counters,
                counters_lock=counters_lock,
                progress_lock=progress_lock,
            )
            futures.append(fut)

        # Wait for all workers to drain.
        for fut in futures:
            try:
                fut.result()
            except Exception as exc:
                print(f"WORKER CRASHED: {exc}", flush=True)
                traceback.print_exc()

    run_wall_sec = time.monotonic() - run_wall_start

    # End-of-run report
    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"  completed:            {counters['completed']}")
    print(f"  skipped (existing):   {counters['skipped']}")
    print(f"  permanent failures:   {counters['failed_perm']}")
    print(f"  transient failures:   {counters['failed_trans']}")
    print(f"  wall time:            {run_wall_sec:.1f}s")
    if counters["audio_sec_total"] > 0:
        rtf = counters["audio_sec_total"] / max(run_wall_sec, 0.001)
        print(
            f"  audio transcribed:    {counters['audio_sec_total']}s "
            f"(RTF = {rtf:.2f}x real-time across {len(worker_servers)} worker(s))"
        )
    print(f"  state file:           {state_path}")

    exit_code = 0 if counters["failed_perm"] == 0 else 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
