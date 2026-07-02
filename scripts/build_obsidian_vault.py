#!/usr/bin/env python3
"""
Build an Obsidian-ready vault from the scraped Maker School tree.

Walks <root>, finds every lesson (dir containing lesson.json + transcript.md),
enriches the transcript with Haiku-generated tags/concepts/summary, appends a
wiki-linked nav block, renames `transcript.md` -> `NN. <Title>.md`, and writes
MOC index files at the module / course / vault level plus concept stubs under
`Concepts/`.  Idempotent: a body-hash stored in frontmatter skips re-tagging;
the nav block lives between sentinel comments and is always regenerated.

USAGE
    python scripts/build_obsidian_vault.py downloads/makerschool
    python scripts/build_obsidian_vault.py downloads/makerschool --dry-run
    python scripts/build_obsidian_vault.py downloads/makerschool --limit 10
    python scripts/build_obsidian_vault.py downloads/makerschool --no-ai
    python scripts/build_obsidian_vault.py downloads/makerschool --git-init

REQUIREMENTS
    pip install anthropic pyyaml
    export ANTHROPIC_API_KEY=...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Load .env from the repo root (two levels up from this file: scripts/build_obsidian_vault.py).
# Non-fatal if python-dotenv isn't installed or .env is missing — env vars already set win either way.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("", "0", "false", "no", "off")


VAULT_SCHEMA = 1
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SPARSE_INPUT_MIN_CHARS = 300  # below this, skip Haiku and derive metadata from path

# ---- Local OpenAI-compatible LLM (llama.cpp / vLLM / LM Studio / Ollama-openai) ----
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "").rstrip("/")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "")
LOCAL_LLM_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "not-needed")
LOCAL_LLM_DISABLE_THINKING = _env_bool("LOCAL_LLM_DISABLE_THINKING", True)
LOCAL_LLM_MAX_TOKENS = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "6000"))
LOCAL_LLM_TEMPERATURE = float(os.environ.get("LOCAL_LLM_TEMPERATURE", "0.2"))
LOCAL_LLM_TIMEOUT = float(os.environ.get("LOCAL_LLM_TIMEOUT", "900"))

# ---- CLI fallback LLMs ----
# Codex is useful as a local authenticated CLI fallback when Claude CLI / SDK is
# unavailable (for example: `claude` logged out). It is intentionally opt-in via
# backend choice or fallback list because it is usually slower/costlier than Haiku.
CODEX_CLI = os.environ.get("CODEX_CLI", "codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_CLI_TIMEOUT = float(os.environ.get("CODEX_CLI_TIMEOUT", "900"))
AI_FALLBACK_BACKENDS = tuple(
    b.strip()
    for b in os.environ.get("AI_FALLBACK_BACKENDS", "codex").split(",")
    if b.strip()
)

# ---- Canonicalization pass (--canonicalize) ----
CANON_BACKEND = os.environ.get("CANON_BACKEND", "sdk")  # sdk|api|local
CANON_MODEL = os.environ.get("CANON_MODEL", "claude-opus-4-7")
CANON_API_BETA_1M = _env_bool(
    "CANON_API_BETA_1M", False
)  # opt-in 1M context header on --canon-backend api
NAV_START = "<!-- vault:nav-start -->"
NAV_END = "<!-- vault:nav-end -->"
RELATED_START = "<!-- vault:related-start -->"
RELATED_END = "<!-- vault:related-end -->"

SYSTEM_PROMPT = """You classify lesson transcripts from the "Maker School" program — a freelancing/automation curriculum covering cold email, Upwork, lead scraping, proposals, sales calls, n8n, Make.com, and agentic workflows.

Given ONE transcript, return STRICT JSON (no prose, no code fences):
{
  "summary": "one sentence, <=160 chars, what the lesson teaches",
  "tags":    ["3-7 kebab-case tags"],
  "concepts":["2-5 Title Case concept names"],
  "aliases": ["0-2 alternative titles or empty list"]
}

Rules:
- Tags: reuse program vocabulary when applicable — cold-email, upwork, lead-generation, scraping, proposals, sales-call, pricing, offer-design, n8n, make, agentic-workflows, mindset, retrospective, accountability, community, portfolio, positioning, infrastructure, tooling. Add 1-2 specific tags when useful.
- Concepts: atomic, reusable ideas that could each become a note (e.g. "Cold Email Deliverability", "Upwork Specialist Profile", "Theory of Constraints"). Use consistent Title Case names so the same idea in different lessons produces the same wikilink.
- No trailing commas. No nulls. Always arrays, even if empty."""


# ---------- data ----------


@dataclass
class Lesson:
    course: str  # "Month 1", "Automation Tutorials", ...
    sub_course: str | None  # e.g. "N8N Accelerator" for Automation Tutorials, else None
    module_dir: str  # "2-Day 1"
    module_title: str  # "Day 1"
    module_index: int
    lesson_dir: Path  # absolute path to lesson folder
    lesson_index: int
    lesson_title: str  # raw from lesson.json
    lesson_id: str
    transcript_path: Path | None  # None if no video / transcript available
    index_html_path: Path | None  # absolute path to index.html if present
    # computed
    has_video: bool = False
    new_filename: str = ""  # e.g. "01. Choose operating name.md"
    new_path: Path = field(default=Path())
    body_hash: str = ""
    ai: dict = field(default_factory=dict)  # {summary, tags, concepts, aliases}
    prev: "Lesson | None" = None
    next: "Lesson | None" = None


# ---------- helpers ----------


ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]]')


def safe_filename(name: str) -> str:
    s = ILLEGAL.sub("", name).strip().rstrip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:180] or "Untitled"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm = yaml.safe_load(text[4:end]) or {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, text[end + 5 :]


def dump_frontmatter(fm: dict) -> str:
    return (
        "---\n"
        + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000)
        + "---\n"
    )


def sha256_short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def strip_nav_and_related(body: str) -> str:
    """Remove any previously-written nav/related blocks so we can rewrite."""
    for start, end in ((NAV_START, NAV_END), (RELATED_START, RELATED_END)):
        pattern = re.compile(
            re.escape(start) + r".*?" + re.escape(end) + r"\n*", re.DOTALL
        )
        body = pattern.sub("", body)
    return body.rstrip() + "\n"


def strip_leading_h1(body: str) -> str:
    """Remove a leading '# ...' heading so we can prepend our own canonical one."""
    lines = body.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).rstrip() + "\n"


def clean_title(t: str) -> str:
    """Strip a leading 'N.' / 'NN.' numeric prefix from a lesson title for display."""
    return re.sub(r"^\s*\d+\.\s*", "", t).strip()


def extract_html_lesson(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """Extract the lesson body (as markdown) and the resources list from an index.html.

    Returns (body_markdown, resources) where resources is a list of (label, href).
    Strips decorative chrome (breadcrumb, page title, back-to-index nav, <style>).
    """
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", []
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("div", class_="content")
    body_md = ""
    if content:
        inner = str(content)
        body_md = markdownify(
            inner, heading_style="ATX", strip=["style", "script"]
        ).strip()
        # markdownify keeps the wrapping div as nothing, and may produce excessive blank lines
        body_md = re.sub(r"\n{3,}", "\n\n", body_md)
    resources: list[tuple[str, str]] = []
    res_div = soup.find("div", class_="resources")
    if res_div:
        for a in res_div.find_all("a"):
            href = (a.get("href") or "").strip()
            label = " ".join(a.get_text(" ", strip=True).split())
            if href and label:
                resources.append((label, href))
    return body_md, resources


def wikilink(path_from_vault: str, display: str | None = None) -> str:
    # Drop .md extension; Obsidian resolves anyway.
    link = path_from_vault
    if link.endswith(".md"):
        link = link[:-3]
    if display and display != os.path.basename(link):
        return f"[[{link}|{display}]]"
    return f"[[{link}]]"


# ---------- scan ----------


COURSE_ORDER = [
    "Pre-Program- Before You Start",
    "Month 1",
    "Month 2",
    "Month 3",
    "Month 4",
    "Month 5",
    "Month 6",
    "Automation Tutorials",
    "Resource Library",
]


def course_sort_key(name: str) -> tuple:
    try:
        return (COURSE_ORDER.index(name), name)
    except ValueError:
        return (999, name)


def scan_lessons(vault_root: Path, course_from_section: bool = True) -> list[Lesson]:
    lessons: list[Lesson] = []
    seen: dict[str, Path] = {}  # lesson_id → first-seen lesson_dir (dedupe defense)
    # sort for deterministic "first seen" across filesystems
    for lesson_json in sorted(vault_root.rglob("lesson.json")):
        lesson_dir = lesson_json.parent
        transcript: Path | None = lesson_dir / "transcript.md"
        # accept an already-renamed file from a prior run
        if not transcript.exists():
            alt = _find_existing_renamed(lesson_dir)
            if alt is not None:
                transcript = alt
            else:
                transcript = None
                for cand in ("transcript.parakeet.md", "transcript.remote.md"):
                    p = lesson_dir / cand
                    if p.exists():
                        transcript = p
                        break
        # lessons with no transcript are still valid if they have index.html content
        idx_html = lesson_dir / "index.html"
        if transcript is None and not idx_html.exists():
            continue
        try:
            meta = json.loads(lesson_json.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"skip {lesson_json}: {e}", file=sys.stderr)
            continue

        # Dedupe by lesson_id — defense against vault duplication (Phase 1 root-cause
        # was fixed in skool-downloader, but this guard prevents recurrence from any source).
        lid = meta.get("lessonId") or meta.get("lesson_id")
        if not lid:
            print(f"  warn: no lessonId in {lesson_json}; skipping", file=sys.stderr)
            continue
        if lid in seen:
            print(
                f"  warn: duplicate lesson_id {lid!r}: keeping {seen[lid]}, skipping {lesson_dir}",
                file=sys.stderr,
            )
            continue
        seen[lid] = lesson_dir

        rel = lesson_dir.relative_to(vault_root)
        parts = rel.parts
        # Structure variants:
        #   <course>/<module_dir>/<lesson_dir>                          (Month N, Pre-Program, Resource Library)
        #   <course>/<sub_course>/<lesson_dir>                          (Automation Tutorials has no module layer)
        if len(parts) < 3:
            continue
        # course derivation:
        #   course_from_section=True (default): immediate child of vault_root = section name
        #     (e.g. "Month 1", "Pre-Program- Before You Start", "Automation Tutorials").
        #   course_from_section=False: legacy uniform value = vault_root folder name
        #     (e.g. "Maker School") for vaults still nested under a single top-level folder.
        course = parts[0] if course_from_section else vault_root.name
        if parts[0] == "Automation Tutorials":
            sub_course = parts[1]  # "2-N8N Accelerator"
            module_dir = parts[1]
            module_title = meta.get("moduleTitle") or re.sub(r"^\d+-", "", sub_course)
            sub_course_clean = re.sub(r"^\d+-", "", sub_course)
        else:
            sub_course_clean = None
            module_dir = parts[1]
            module_title = meta.get("moduleTitle") or re.sub(r"^\d+-", "", module_dir)

        lessons.append(
            Lesson(
                course=course,
                sub_course=sub_course_clean,
                module_dir=module_dir,
                module_title=module_title,
                module_index=int(meta.get("moduleIndex", 0)),
                lesson_dir=lesson_dir,
                lesson_index=int(meta.get("lessonIndex", 0)),
                lesson_title=meta.get("title") or lesson_dir.name,
                lesson_id=meta.get("lessonId", ""),
                transcript_path=transcript,
                index_html_path=idx_html if idx_html.exists() else None,
                has_video=bool(meta.get("hasVideo")),
            )
        )
    return lessons


def _find_existing_renamed(lesson_dir: Path) -> Path | None:
    """A prior run may have renamed transcript.md -> 'NN. Title.md'. Detect it."""
    for p in lesson_dir.glob("*.md"):
        if p.name in (
            "transcript.md",
            "transcript.remote.md",
            "transcript.parakeet.md",
        ):
            continue
        # Heuristic: starts with digits + '.' + space
        if re.match(r"^\d{2}\. ", p.name):
            return p
    return None


# ---------- neighbors ----------


def link_neighbors(lessons: list[Lesson]) -> None:
    buckets: dict[tuple, list[Lesson]] = defaultdict(list)
    for L in lessons:
        buckets[(L.course, L.module_dir)].append(L)
    for bucket in buckets.values():
        bucket.sort(key=lambda x: (x.lesson_index, x.lesson_title))
        for i, L in enumerate(bucket):
            L.prev = bucket[i - 1] if i > 0 else None
            L.next = bucket[i + 1] if i < len(bucket) - 1 else None


# ---------- AI ----------


def _coerce_classification(data: dict) -> dict:
    return {
        "summary": str(data.get("summary", ""))[:200],
        "tags": [
            str(t).strip().lower().replace(" ", "-") for t in data.get("tags", []) if t
        ][:7],
        "concepts": [str(c).strip() for c in data.get("concepts", []) if c][:5],
        "aliases": [str(a).strip() for a in data.get("aliases", []) if a][:2],
    }


def _parse_classification_text(text: str) -> dict:
    t = text.strip()
    # strip ``` fences (with or without 'json')
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    # the model sometimes emits prose before/after JSON — find the outermost {...}
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        t = m.group(0)
    return _coerce_classification(json.loads(t))


def _build_user_msg(title: str, path_hint: str, body: str) -> str:
    max_chars = 16000
    snippet = (
        body if len(body) <= max_chars else body[:max_chars] + "\n\n[...truncated...]"
    )
    return f"LESSON PATH: {path_hint}\nTITLE: {title}\n\nTRANSCRIPT:\n{snippet}"


# --- Raw Anthropic API path (fast, requires ANTHROPIC_API_KEY) ---


def make_api_client():
    import anthropic

    return anthropic.Anthropic()


def haiku_classify_api(client, title: str, path_hint: str, body: str) -> dict:
    import anthropic

    user_msg = _build_user_msg(title, path_hint, body)
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=400,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            )
            return _parse_classification_text(resp.content[0].text)
        except (
            anthropic.APIError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            ValueError,
        ) as e:
            if attempt == 2:
                print(f"  haiku(api) failed ({e}) for {title}", file=sys.stderr)
                return {"summary": "", "tags": [], "concepts": [], "aliases": []}


# --- Local OpenAI-compatible path (llama.cpp / vLLM / LM Studio / Ollama-openai) ---


def classify_local_api(title: str, path_hint: str, body: str) -> dict:
    """Synchronous classify via a local /v1/chat/completions endpoint.

    Safe to call from threads. Reads config from LOCAL_LLM_* env vars.
    On thinking-capable models (Qwen3, QwQ, etc.) sets chat_template_kwargs to
    disable <think> blocks when LOCAL_LLM_DISABLE_THINKING is truthy.
    """
    import requests

    if not LOCAL_LLM_BASE_URL or not LOCAL_LLM_MODEL:
        print(
            f"  local(cfg) LOCAL_LLM_BASE_URL/MODEL not set; returning empty for {title}",
            file=sys.stderr,
        )
        return {"summary": "", "tags": [], "concepts": [], "aliases": []}

    user_msg = _build_user_msg(title, path_hint, body)
    payload: dict[str, Any] = {
        "model": LOCAL_LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": LOCAL_LLM_TEMPERATURE,
        "max_tokens": LOCAL_LLM_MAX_TOKENS,
    }
    if LOCAL_LLM_DISABLE_THINKING:
        # llama.cpp + vLLM both honor chat_template_kwargs.enable_thinking for Qwen3 family.
        # Servers that don't recognize it ignore it (tested on llama.cpp b8605).
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"Content-Type": "application/json"}
    if LOCAL_LLM_API_KEY and LOCAL_LLM_API_KEY != "not-needed":
        headers["Authorization"] = f"Bearer {LOCAL_LLM_API_KEY}"

    for attempt in range(3):
        try:
            r = requests.post(
                f"{LOCAL_LLM_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=LOCAL_LLM_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            msg = data["choices"][0]["message"]
            raw = (msg.get("content") or "").strip()
            if not raw:
                # some thinking-model servers return content in reasoning_content when the
                # answer block gets truncated — don't try to parse it, trigger a retry.
                raise ValueError(
                    "empty content (model may have exhausted max_tokens on reasoning)"
                )
            return _parse_classification_text(raw)
        except Exception as e:
            if attempt == 2:
                print(
                    f"  local({LOCAL_LLM_MODEL}) failed ({e}) for {title}",
                    file=sys.stderr,
                )
                return {"summary": "", "tags": [], "concepts": [], "aliases": []}


# --- Hybrid routing ---

# Default path globs that route to Haiku in hybrid mode: technical/tool-heavy lessons
# where specific concept names (e.g. "Array Explosion And Collapse", "Hook Deck Rate Limiting")
# empirically beat the local model's more generic output. Override via --haiku-paths.
DEFAULT_HAIKU_PATH_GLOBS = ("Automation Tutorials/*", "*N8N*", "*n8n*")


def _lesson_path_segments(L: "Lesson") -> list[str]:
    parts = [L.course, L.sub_course or "", L.module_title, L.lesson_title]
    return [p for p in parts if p]


def pick_backend_for_lesson(
    L: "Lesson", mode: str, haiku_globs: tuple[str, ...]
) -> str:
    """Return which backend to use for this lesson.

    Backends: sdk | api | local | codex | none. `mode` is the user's backend
    choice. In hybrid mode, lessons whose course/module/title matches any glob
    go to Haiku (sdk or api depending on env), the rest go to local.
    """
    if mode in ("sdk", "api", "local", "codex", "none"):
        return mode
    if mode == "hybrid":
        import fnmatch

        haystack = " / ".join(_lesson_path_segments(L))
        for g in haiku_globs:
            if fnmatch.fnmatch(haystack, f"*{g}*"):
                # Prefer api when a key is available, else sdk (Max subscription via CLI).
                return "api" if os.environ.get("ANTHROPIC_API_KEY") else "sdk"
        return "local"
    return mode  # fallthrough (shouldn't happen)


def _empty_ai() -> dict:
    return {"summary": "", "tags": [], "concepts": [], "aliases": []}


def _is_empty_ai(ai: dict | None) -> bool:
    return (
        not ai or not ai.get("summary") or not ai.get("tags") or not ai.get("concepts")
    )


# --- Codex CLI fallback path (uses Codex CLI auth, no Anthropic key) ---


def classify_codex_cli(title: str, path_hint: str, body: str) -> dict:
    """Synchronous classify via `codex exec`.

    This is a fallback for when Claude CLI / Agent SDK is unavailable. It writes
    Codex's final answer to a temp file and parses that file as strict JSON.
    """
    import tempfile

    user_msg = _build_user_msg(title, path_hint, body)
    prompt = (
        "You are a strict JSON classification function. Follow this system prompt exactly.\n\n"
        f"SYSTEM PROMPT:\n{SYSTEM_PROMPT}\n\n"
        "Return only the JSON object, with no Markdown fences and no prose.\n\n"
        f"USER INPUT:\n{user_msg}"
    )

    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    cmd = [
        CODEX_CLI,
        "exec",
        "-",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--output-last-message",
        output_path,
    ]
    if CODEX_MODEL:
        cmd.extend(["--model", CODEX_MODEL])

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=CODEX_CLI_TIMEOUT,
            check=False,
        )
        raw = Path(output_path).read_text(encoding="utf-8", errors="ignore").strip()
        if proc.returncode != 0:
            raise RuntimeError(
                (proc.stderr or "").strip() or f"codex exited {proc.returncode}"
            )
        if not raw:
            raise ValueError("empty response from codex")
        return _parse_classification_text(raw)
    except Exception as e:
        print(f"  codex(cli) failed ({e}) for {title}", file=sys.stderr)
        return _empty_ai()
    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except Exception:
            pass


# --- Claude Agent SDK path (uses Claude CLI / Max subscription, no API key) ---


async def haiku_classify_sdk(title: str, path_hint: str, body: str) -> dict:
    import asyncio
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

    opts = ClaudeAgentOptions(
        model=HAIKU_MODEL,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        max_turns=1,
    )
    user_msg = _build_user_msg(title, path_hint, body)
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            text = ""
            async for msg in query(prompt=user_msg, options=opts):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            text += b.text
            if not text.strip():
                raise ValueError("empty response from CLI")
            return _parse_classification_text(text)
        except Exception as e:  # SDK raises bare Exception on CLI exit!=0
            last_err = e
            if attempt < 4:
                await asyncio.sleep(2**attempt + (attempt * 0.3))  # 1, 2.3, 4.6, 9.9 s
                continue
    print(
        f"  haiku(sdk) gave up after 5 tries ({last_err}) for {title}", file=sys.stderr
    )
    return _empty_ai()


async def run_async_enrichment(
    lessons: list["Lesson"],
    force: bool,
    mode: str,
    sdk_concurrency: int,
    local_concurrency: int,
    haiku_globs: tuple[str, ...],
) -> None:
    """Populate L.ai for every lesson.

    Async-driven dispatcher: each lesson is routed via `pick_backend_for_lesson`
    to sdk (Claude CLI, async), api (Anthropic SDK, sync-in-thread), local
    (OpenAI-compat, sync-in-thread) or none. Separate semaphores bound concurrency
    per backend so the local GPU and the Claude CLI don't contend.
    """
    import asyncio

    sdk_sem = asyncio.Semaphore(sdk_concurrency)
    local_sem = asyncio.Semaphore(local_concurrency)
    api_client = None  # lazy init
    done = 0
    lock = asyncio.Lock()
    total = len(lessons)
    backend_counts: dict[str, int] = defaultdict(int)

    async def _one(L: "Lesson") -> None:
        nonlocal done, api_client
        fm, transcript_body, html_body_md, _res, hash_input = _read_lesson_content(L)
        L.body_hash = sha256_short(hash_input)
        cached = (
            fm.get("vault_schema") == VAULT_SCHEMA
            and fm.get("body_sha") == L.body_hash
            and fm.get("tags")
        )
        if cached and not force:
            L.ai = {
                "summary": fm.get("summary", ""),
                "tags": list(fm.get("tags", [])),
                "concepts": list(fm.get("concepts", [])),
                "aliases": list(fm.get("aliases", [])),
            }
            backend_counts["cached"] += 1
        else:
            combined_len = len(html_body_md) + len(transcript_body)
            if combined_len < SPARSE_INPUT_MIN_CHARS:
                L.ai = _derive_sparse_ai(L, len(_res))
                backend_counts["sparse"] += 1
            else:
                hint = f"{L.course}/{L.sub_course or L.module_title}"
                ai_input = (
                    ("LESSON NOTES:\n" + html_body_md + "\n\n") if html_body_md else ""
                ) + (("TRANSCRIPT:\n" + transcript_body) if transcript_body else "")
                picked = pick_backend_for_lesson(L, mode, haiku_globs)
                backend_counts[picked] += 1
                if picked == "sdk":
                    async with sdk_sem:
                        L.ai = await haiku_classify_sdk(L.lesson_title, hint, ai_input)
                    if _is_empty_ai(L.ai):
                        for fallback in AI_FALLBACK_BACKENDS:
                            if fallback == "codex":
                                backend_counts["fallback:codex"] += 1
                                async with sdk_sem:
                                    L.ai = await asyncio.to_thread(
                                        classify_codex_cli,
                                        L.lesson_title,
                                        hint,
                                        ai_input,
                                    )
                            elif fallback == "local":
                                backend_counts["fallback:local"] += 1
                                async with local_sem:
                                    L.ai = await asyncio.to_thread(
                                        classify_local_api,
                                        L.lesson_title,
                                        hint,
                                        ai_input,
                                    )
                            elif fallback == "api" and os.environ.get(
                                "ANTHROPIC_API_KEY"
                            ):
                                backend_counts["fallback:api"] += 1
                                if api_client is None:
                                    api_client = make_api_client()
                                async with sdk_sem:
                                    L.ai = await asyncio.to_thread(
                                        haiku_classify_api,
                                        api_client,
                                        L.lesson_title,
                                        hint,
                                        ai_input,
                                    )
                            if not _is_empty_ai(L.ai):
                                break
                elif picked == "codex":
                    async with sdk_sem:
                        L.ai = await asyncio.to_thread(
                            classify_codex_cli, L.lesson_title, hint, ai_input
                        )
                elif picked == "local":
                    async with local_sem:
                        L.ai = await asyncio.to_thread(
                            classify_local_api, L.lesson_title, hint, ai_input
                        )
                elif picked == "api":
                    if api_client is None:
                        api_client = make_api_client()
                    # API calls run serially within this coroutine but many coroutines may run;
                    # use sdk_sem as a shared cap to avoid hammering the endpoint.
                    async with sdk_sem:
                        L.ai = await asyncio.to_thread(
                            haiku_classify_api,
                            api_client,
                            L.lesson_title,
                            hint,
                            ai_input,
                        )
                else:  # "none"
                    L.ai = {
                        "summary": fm.get("summary", ""),
                        "tags": list(fm.get("tags", [])),
                        "concepts": list(fm.get("concepts", [])),
                        "aliases": list(fm.get("aliases", [])),
                    }
                if _is_empty_ai(L.ai) and picked != "none":
                    backend_counts["empty-ai"] += 1
        async with lock:
            done += 1
            if done % 10 == 0 or done == total:
                print(f"  {done}/{total}")

    results = await asyncio.gather(*[_one(L) for L in lessons], return_exceptions=True)
    errs = [r for r in results if isinstance(r, Exception)]
    if errs:
        print(
            f"  {len(errs)} task(s) raised (already retried internally); first: {errs[0]!r}",
            file=sys.stderr,
        )
    print(
        "  backend routing: "
        + ", ".join(f"{k}={v}" for k, v in sorted(backend_counts.items()))
    )


# Back-compat shim: old name used elsewhere in this file.
async def run_sdk_enrichment(
    lessons: list["Lesson"], force: bool, concurrency: int
) -> None:
    await run_async_enrichment(
        lessons,
        force,
        mode="sdk",
        sdk_concurrency=concurrency,
        local_concurrency=2,
        haiku_globs=DEFAULT_HAIKU_PATH_GLOBS,
    )


# ---------- write ----------


def _read_lesson_content(
    L: "Lesson",
) -> tuple[dict, str, str, list[tuple[str, str]], str]:
    """Read all lesson sources. Returns (transcript_frontmatter, transcript_body, html_body_md, resources, hash_input)."""
    fm: dict = {}
    transcript_body = ""
    if L.transcript_path and L.transcript_path.exists():
        raw = L.transcript_path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        body = strip_leading_h1(strip_nav_and_related(body)).strip()
        is_vault_file = fm.get("vault_schema") == VAULT_SCHEMA
        if is_vault_file:
            # Previously rendered by us: extract ONLY the transcript section.
            # Missing section = no transcript existed at render time.
            m = re.search(
                r"(?ms)^##\s+Transcript\s*\n+(.*?)(?=\n##\s|\n<!--\s*vault:|\Z)", body
            )
            transcript_body = m.group(1).strip() if m else ""
        else:
            # Raw transcript.md: body may have a leading blockquote summary line from a prior partial render — strip it.
            transcript_body = re.sub(r"^\s*>\s.*\n+", "", body).strip()
    html_body_md = ""
    resources: list[tuple[str, str]] = []
    if L.index_html_path and L.index_html_path.exists():
        html_body_md, resources = extract_html_lesson(L.index_html_path)
    # include resource files from lesson_dir/resources/
    res_dir = L.lesson_dir / "resources"
    if res_dir.is_dir():
        for p in sorted(res_dir.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                resources.append((p.name, f"resources/{p.name}"))
    hash_input = f"HTML::{html_body_md}\nTRANSCRIPT::{transcript_body}"
    return fm, transcript_body, html_body_md, resources, hash_input


def _derive_sparse_ai(L: "Lesson", n_resources: int) -> dict:
    """Synthesize minimal metadata for lessons with no transcript and no HTML body
    (e.g. resource-only blueprints). Keeps MOCs linkable without blocking on Haiku."""
    ctx = f"{L.course} {L.sub_course or ''} {L.module_title}".lower()
    tags: list[str] = []
    if "n8n" in ctx:
        tags.append("n8n")
    if "make" in ctx:
        tags.append("make")
    if "agentic" in ctx:
        tags.append("agentic-workflows")
    if "community" in ctx or "call" in ctx:
        tags.append("community-call")
    if "template" in ctx or "blueprint" in ctx:
        tags.append("template")
    if "resource" in ctx or "library" in ctx:
        tags.append("resource")
    if "niche" in ctx or "pack" in ctx:
        tags.append("niche-pack")
    if "sales" in ctx:
        tags.append("sales-training")
    if "vibe" in ctx or "coding" in ctx:
        tags.append("vibe-coding")
    tags.append("reference")
    # Deduplicate while preserving order
    tags = list(dict.fromkeys(tags))[:5]

    concept = L.module_title.strip().strip(":").strip() or L.course
    # Prefer a more specific concept for known shapes
    if "blueprint" in ctx:
        concept = "N8N Blueprint Library"
    elif "niche" in ctx and "pack" in ctx:
        concept = "Niche Packs"
    elif "community" in ctx and "exclusive" in ctx:
        concept = "Community Exclusives"
    elif "community" in ctx and "call" in ctx:
        concept = "Community Calls"

    summary = f"Reference entry in {L.course} › {L.module_title}."
    if n_resources:
        summary += (
            f" Includes {n_resources} downloadable resource"
            + ("s" if n_resources != 1 else "")
            + "."
        )
    return {
        "summary": summary[:200],
        "tags": tags,
        "concepts": [concept],
        "aliases": [],
    }


def _finalize_filename(L: "Lesson") -> None:
    pad = f"{L.lesson_index:02d}"
    fname_title = safe_filename(clean_title(L.lesson_title))
    L.new_filename = f"{pad}. {fname_title}.md"
    L.new_path = L.lesson_dir / L.new_filename


def process_lesson(
    L: Lesson, vault_root: Path, client, no_ai: bool, force: bool, dry_run: bool
) -> None:
    fm, transcript_body, html_body_md, _resources, hash_input = _read_lesson_content(L)
    L.body_hash = sha256_short(hash_input)

    already_tagged = (
        fm.get("vault_schema") == VAULT_SCHEMA
        and fm.get("body_sha") == L.body_hash
        and fm.get("tags")
        and fm.get("concepts") is not None
    )
    if already_tagged and not force:
        L.ai = {
            k: list(fm.get(k, [])) if k != "summary" else fm.get(k, "")
            for k in ("summary", "tags", "concepts", "aliases")
        }
    elif no_ai:
        L.ai = {
            k: list(fm.get(k, [])) if k != "summary" else fm.get(k, "")
            for k in ("summary", "tags", "concepts", "aliases")
        }
    else:
        combined_len = len(html_body_md) + len(transcript_body)
        if combined_len < SPARSE_INPUT_MIN_CHARS:
            L.ai = _derive_sparse_ai(L, len(_resources))
        else:
            hint = f"{L.course}/{L.sub_course or L.module_title}"
            ai_input = (
                ("LESSON NOTES:\n" + html_body_md + "\n\n") if html_body_md else ""
            ) + (("TRANSCRIPT:\n" + transcript_body) if transcript_body else "")
            L.ai = haiku_classify_api(client, L.lesson_title, hint, ai_input)

    _finalize_filename(L)


def render_lesson(
    L: Lesson, vault_root: Path, concept_paths: dict[str, Path]
) -> tuple[Path, str]:
    fm, transcript_body, html_body_md, resources, _hash_input = _read_lesson_content(L)
    title_display = clean_title(L.lesson_title)

    # Build enriched frontmatter (preserve original transcription metadata)
    new_fm = {
        "vault_schema": VAULT_SCHEMA,
        "title": L.lesson_title,
        "aliases": L.ai.get("aliases", []),
        "course": L.course,
        **({"sub_course": L.sub_course} if L.sub_course else {}),
        "module": L.module_title,
        "module_index": L.module_index,
        "lesson_index": L.lesson_index,
        "lesson_id": L.lesson_id,
        "summary": L.ai.get("summary", ""),
        "tags": L.ai.get("tags", []),
        "concepts": L.ai.get("concepts", []),
        "body_sha": L.body_hash,
    }
    # carry forward transcription provenance if present
    for k in (
        "source",
        "transcribed_at",
        "model",
        "language",
        "duration_sec",
        "word_count",
    ):
        if k in fm:
            new_fm[k] = fm[k]

    # Build nav + related blocks
    def link_lesson(o: Lesson) -> str:
        rel = (o.lesson_dir / o.new_filename).relative_to(vault_root).as_posix()
        return wikilink(rel, f"{o.lesson_index:02d}. {clean_title(o.lesson_title)}")

    module_moc_rel = (
        (L.lesson_dir.parent / f"_{safe_filename(L.module_title)}.md")
        .relative_to(vault_root)
        .as_posix()
    )
    nav_parts = []
    if L.prev:
        nav_parts.append(f"← {link_lesson(L.prev)}")
    nav_parts.append(f"⇡ {wikilink(module_moc_rel, L.module_title)}")
    if L.next:
        nav_parts.append(f"{link_lesson(L.next)} →")
    nav_block = f"{NAV_START}\n**Nav:** " + " · ".join(nav_parts) + f"\n{NAV_END}"

    related_block = ""
    if L.ai.get("concepts"):
        links = []
        for c in L.ai["concepts"]:
            cpath = concept_paths.get(c)
            if cpath:
                links.append(wikilink(cpath.relative_to(vault_root).as_posix(), c))
        if links:
            related_block = (
                f"\n\n{RELATED_START}\n## Related concepts\n- "
                + "\n- ".join(links)
                + f"\n{RELATED_END}"
            )

    h1 = f"# {title_display}\n"
    summary_line = f"\n> {L.ai['summary']}\n" if L.ai.get("summary") else ""
    sections: list[str] = []
    if html_body_md:
        sections.append("## Lesson notes\n\n" + html_body_md)
    if transcript_body:
        label = "## Transcript" + ("" if L.has_video else "")
        sections.append(label + "\n\n" + transcript_body)
    if resources:
        res_lines = ["## Resources", ""]
        for label, href in resources:
            res_lines.append(f"- [{label}]({href})")
        sections.append("\n".join(res_lines))
    if not sections:
        sections.append("_No content captured for this lesson._")
    body_full = "\n\n".join(sections)
    content = (
        dump_frontmatter(new_fm)
        + "\n"
        + h1
        + summary_line
        + "\n"
        + body_full
        + "\n\n"
        + nav_block
        + related_block
        + "\n"
    )
    return L.new_path, content


# ---------- MOCs ----------


def write_mocs(
    lessons: list[Lesson], vault_root: Path, dry_run: bool
) -> dict[str, Path]:
    # group
    by_module: dict[tuple[str, str], list[Lesson]] = defaultdict(list)
    by_course: dict[str, list[Lesson]] = defaultdict(list)
    concepts: dict[str, list[Lesson]] = defaultdict(list)

    # Case-insensitive concept dedupe: macOS/Windows have case-insensitive
    # filesystems, so "Simplicity as X" and "Simplicity As X" would clobber
    # each other when stubs are written. Collapse to the first-seen casing.
    canonical_case: dict[str, str] = {}
    for L in lessons:
        by_module[(L.course, L.module_dir)].append(L)
        by_course[L.course].append(L)
        for c in L.ai.get("concepts", []):
            key = canonical_case.setdefault(c.lower(), c)
            concepts[key].append(L)

    moc_files: list[tuple[Path, str]] = []

    # Module / Day MOCs — auto-managed, regenerated on every build.
    # Body explicitly marks them as auto-generated to discourage hand edits.
    AUTO_NOTE = "<!-- auto-generated by build_obsidian_vault.py — do not hand-edit -->"
    for (course, module_dir), ls in by_module.items():
        ls_sorted = sorted(ls, key=lambda x: (x.lesson_index, x.lesson_title))
        module_title = ls_sorted[0].module_title
        moc_path = (
            vault_root / course / module_dir / f"_{safe_filename(module_title)}.md"
        )
        lines = [
            AUTO_NOTE,
            "",
            f"# {module_title}\n",
            f"> Course: [[{course}/_{safe_filename(course)}|{course}]]\n",
            "## Lessons\n",
        ]
        for L in ls_sorted:
            rel = (L.new_path).relative_to(vault_root).as_posix()
            lines.append(
                f"- {wikilink(rel, f'{L.lesson_index:02d}. {clean_title(L.lesson_title)}')}"
            )
            if L.ai.get("summary"):
                lines.append(f"    - {L.ai['summary']}")
        fm = {
            "vault_schema": VAULT_SCHEMA,
            "title": module_title,
            "type": "module-moc",  # legacy key (back-compat with existing readers)
            "moc_type": "module",  # new canonical key
            "course": course,
            "module": module_title,
        }
        moc_files.append(
            (moc_path, dump_frontmatter(fm) + "\n" + "\n".join(lines) + "\n")
        )

    # Filesystem sweep: emit a stub MOC for every module folder that has NO scanned
    # lessons (ensures Day folders never silently lose their MOC, e.g. video-less days).
    seen_module_paths = {(vault_root / c / m).resolve() for (c, m) in by_module.keys()}
    for course in by_course.keys():
        course_dir = vault_root / course
        if not course_dir.is_dir():
            continue
        for module_dir_path in sorted(p for p in course_dir.iterdir() if p.is_dir()):
            if module_dir_path.resolve() in seen_module_paths:
                continue
            module_title = re.sub(r"^\d+-", "", module_dir_path.name)
            moc_path = module_dir_path / f"_{safe_filename(module_title)}.md"
            lines = [
                AUTO_NOTE,
                "",
                f"# {module_title}\n",
                f"> Course: [[{course}/_{safe_filename(course)}|{course}]]\n",
                "_No lessons indexed for this module yet._\n",
            ]
            fm = {
                "vault_schema": VAULT_SCHEMA,
                "title": module_title,
                "type": "module-moc",
                "moc_type": "module",
                "course": course,
                "module": module_title,
            }
            moc_files.append(
                (moc_path, dump_frontmatter(fm) + "\n" + "\n".join(lines) + "\n")
            )

    # Course MOCs
    for course, ls in by_course.items():
        moc_path = vault_root / course / f"_{safe_filename(course)}.md"
        modules = sorted({(L.module_index, L.module_dir, L.module_title) for L in ls})
        lines = [
            f"# {course}\n",
            f"> [[Maker School]] › **{course}**\n",
            "## Modules\n",
        ]
        for _, mdir, mtitle in modules:
            mrel = (
                (vault_root / course / mdir / f"_{safe_filename(mtitle)}.md")
                .relative_to(vault_root)
                .as_posix()
            )
            lines.append(f"- {wikilink(mrel, mtitle)}")
        fm = {
            "vault_schema": VAULT_SCHEMA,
            "title": course,
            "type": "course-moc",
            "course": course,
        }
        moc_files.append(
            (moc_path, dump_frontmatter(fm) + "\n" + "\n".join(lines) + "\n")
        )

    # Root MOC
    root_path = vault_root / "Maker School.md"
    lines = [
        "# Maker School\n",
        "> Root of the vault. Each course section is a MOC.\n",
        "## Sections\n",
    ]
    for course in sorted(by_course.keys(), key=course_sort_key):
        crel = (
            (vault_root / course / f"_{safe_filename(course)}.md")
            .relative_to(vault_root)
            .as_posix()
        )
        lines.append(f"- {wikilink(crel, course)}")
    lines.append("\n## Concepts\n")
    for c in sorted(concepts.keys()):
        crel = f"Concepts/{safe_filename(c)}.md"
        lines.append(f"- {wikilink(crel, c)}")
    fm = {"vault_schema": VAULT_SCHEMA, "title": "Maker School", "type": "vault-root"}
    moc_files.append((root_path, dump_frontmatter(fm) + "\n" + "\n".join(lines) + "\n"))

    # Concept stubs
    concepts_dir = vault_root / "Concepts"
    concept_paths: dict[str, Path] = {}
    for c, ls in concepts.items():
        cpath = concepts_dir / f"{safe_filename(c)}.md"
        concept_paths[c] = cpath
        ls_sorted = sorted(ls, key=lambda L: (L.course, L.module_index, L.lesson_index))
        lines = [
            f"# {c}\n",
            f"> Concept referenced in {len(ls)} lesson"
            + ("s" if len(ls) != 1 else "")
            + ".\n",
            "## Lessons\n",
        ]
        for L in ls_sorted:
            rel = L.new_path.relative_to(vault_root).as_posix()
            lines.append(
                f"- {wikilink(rel, f'{L.course} › {L.module_title} › {clean_title(L.lesson_title)}')}"
            )
        fm = {
            "vault_schema": VAULT_SCHEMA,
            "title": c,
            "type": "concept",
            "tags": ["concept"],
            "lesson_count": len(ls),
        }
        moc_files.append((cpath, dump_frontmatter(fm) + "\n" + "\n".join(lines) + "\n"))

    # Persist
    if not dry_run:
        for path, content in moc_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print(f"  wrote {len(moc_files)} MOC/concept files")

    # Cleanup: remove orphan auto-generated MOCs left over from prior layouts
    # (e.g. section-level "Month 1/_Day 3.md" stubs from pre-consolidation builds
    # when vault_root pointed at the parent of "Maker School/"). We only remove
    # files that carry the AUTO_NOTE marker AND aren't in the expected set, so
    # hand-written MOCs are never touched.
    expected = {p.resolve() for p, _ in moc_files}
    removed = 0
    for course in by_course.keys():
        course_dir = vault_root / course
        if not course_dir.is_dir():
            continue
        for f in course_dir.glob("_*.md"):
            if f.resolve() in expected:
                continue
            try:
                text = f.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
                # Auto-managed MOCs carry either the AUTO_NOTE marker (newer builds)
                # or a moc_type/type:module-moc frontmatter (older builds before
                # AUTO_NOTE existed — these are the section-level orphans we want).
                is_auto = (
                    AUTO_NOTE in text
                    or fm.get("moc_type") == "module"
                    or fm.get("type") == "module-moc"
                )
                if is_auto:
                    if not dry_run:
                        f.unlink()
                    removed += 1
            except Exception:
                pass
    if removed:
        print(f"  removed {removed} orphan auto-generated MOC(s) from prior layouts")
    return concept_paths


# ---------- git ----------


GITIGNORE = """# skool-downloader artifacts — exclude heavy/non-vault files
*.mp4
*.mov
*.webm
*.mkv
*.m4a
*.mp3
*.wav

# Original scrape artifacts (kept on disk for re-transcription, not versioned)
index.html
lesson.json
video_fingerprint.json
assets/
resources/

# Original & alt transcripts (canonical is the renamed NN. Title.md)
transcript.md
transcript.remote.md
transcript.parakeet.md

# System
.DS_Store
.obsidian/workspace*
.obsidian/cache
"""


def git_init(vault_root: Path, dry_run: bool) -> None:
    gi = vault_root / ".gitignore"
    if dry_run:
        print(f"  [dry-run] would write {gi}")
        return
    gi.write_text(GITIGNORE, encoding="utf-8")
    if (vault_root / ".git").exists():
        print("  .git already present — skipping init")
    else:
        subprocess.run(["git", "init", "-q"], cwd=vault_root, check=True)
        print(f"  git init in {vault_root}")
    # initial add+commit only if there are staged changes
    subprocess.run(["git", "add", "-A"], cwd=vault_root, check=True)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=vault_root)
    if r.returncode != 0:
        subprocess.run(
            [
                "git",
                "commit",
                "-q",
                "-m",
                "vault: build obsidian structure (MOCs, nav, tags)",
            ],
            cwd=vault_root,
            check=True,
        )
        print("  committed vault snapshot")
    else:
        print("  no changes to commit")


# ---------- canonicalization ----------


CANON_SYSTEM_PROMPT = """You are a knowledge-graph librarian. You receive the full inventory of tag and concept names used across a single vault of lesson notes, along with their occurrence counts. The vault is a freelancing / automation curriculum (cold email, Upwork, lead scraping, proposals, sales calls, n8n, Make.com, agentic workflows, mindset, retrospectives).

Your job is to produce a canonicalization mapping that merges near-duplicates so the wikilink graph becomes coherent.

Rules:
- Merge trivial variants (case, plural, whitespace, adjective reordering, obvious synonyms) into ONE canonical spelling.
- Merge concepts that describe the SAME underlying idea expressed differently (e.g. "Weekly Retrospective Cycle" / "Retrospective Framework" / "Retrospective Analysis Framework" -> "Weekly Retrospective Framework"). Use your judgment: merge aggressively when the ideas are the same; KEEP distinct when they're truly different topics even if the words overlap.
- For tags, strongly prefer names from the curated vocabulary when a variant means the same thing: cold-email, upwork, lead-generation, scraping, proposals, sales-call, pricing, offer-design, n8n, make, agentic-workflows, mindset, retrospective, accountability, community, portfolio, positioning, infrastructure, tooling.
- Canonical names should be the clearest, most self-descriptive spelling among the variants (use Title Case for concepts, kebab-case for tags).
- The mapping MUST be idempotent: every canonical name must also map to itself.
- Do not invent new names that aren't in the input.

Return STRICT JSON, no prose, no code fences:
{
  "concepts": { "<variant>": "<canonical>", ... },
  "tags":     { "<variant>": "<canonical>", ... },
  "notes":    "<=400 chars, what merges you made and why"
}
"""


def _collect_vault_inventory(
    vault_root: Path,
) -> tuple[
    dict[str, list[Path]],
    dict[str, int],
    dict[str, list[Path]],
    dict[str, int],
    list[Path],
]:
    """Walk the vault, parse frontmatter of every lesson file.

    Returns (concept_to_paths, concept_counts, tag_to_paths, tag_counts, lesson_files).
    A "lesson file" is any .md with vault_schema:1 in its frontmatter.
    """
    concept_paths: dict[str, list[Path]] = defaultdict(list)
    tag_paths: dict[str, list[Path]] = defaultdict(list)
    concept_counts: dict[str, int] = defaultdict(int)
    tag_counts: dict[str, int] = defaultdict(int)
    lesson_files: list[Path] = []
    seen_ids: dict[str, Path] = {}  # lesson_id → first-seen path (dedupe defense)
    for p in sorted(vault_root.rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if not text.startswith("---\n"):
            continue
        fm, _body = parse_frontmatter(text)
        if fm.get("vault_schema") != VAULT_SCHEMA:
            continue
        # Skip MOCs — only count true lesson notes for dedupe and inventory.
        if fm.get("type") in (
            "module-moc",
            "course-moc",
            "vault-root",
            "concept",
        ) or fm.get("moc_type"):
            continue
        # Dedupe by lesson_id (defense-in-depth against vault duplication).
        lid = fm.get("lesson_id") or fm.get("lessonId")
        if lid:
            if lid in seen_ids:
                print(
                    f"  warn: duplicate lesson_id {lid!r}: keeping {seen_ids[lid]}, skipping {p}",
                    file=sys.stderr,
                )
                continue
            seen_ids[lid] = p
        lesson_files.append(p)
        for c in fm.get("concepts", []) or []:
            c = str(c).strip()
            if c:
                concept_paths[c].append(p)
                concept_counts[c] += 1
        for t in fm.get("tags", []) or []:
            t = str(t).strip()
            if t:
                tag_paths[t].append(p)
                tag_counts[t] += 1
    return concept_paths, concept_counts, tag_paths, tag_counts, lesson_files


def _build_canon_prompt(
    concept_counts: dict[str, int], tag_counts: dict[str, int]
) -> str:
    concept_lines = [
        f"  {c!r}: {n}"
        for c, n in sorted(concept_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    tag_lines = [
        f"  {t!r}: {n}"
        for t, n in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return (
        "VAULT CONCEPT INVENTORY (name: occurrences across lessons):\n"
        + "\n".join(concept_lines)
        + "\n\nVAULT TAG INVENTORY (name: occurrences across lessons):\n"
        + "\n".join(tag_lines)
        + "\n\nReturn the canonicalization mapping as specified in the system prompt."
    )


def _canon_call_sdk(user_msg: str) -> str:
    import asyncio
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

    async def _run() -> str:
        opts = ClaudeAgentOptions(
            model=CANON_MODEL,
            system_prompt=CANON_SYSTEM_PROMPT,
            allowed_tools=[],
            max_turns=1,
        )
        text = ""
        async for msg in query(prompt=user_msg, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text += b.text
        return text

    return asyncio.run(_run())


def _canon_call_api(user_msg: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    extra_headers = {}
    if CANON_API_BETA_1M:
        extra_headers["anthropic-beta"] = "context-1m-2025-08-07"
    resp = client.messages.create(
        model=CANON_MODEL,
        max_tokens=8000,
        system=CANON_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        extra_headers=extra_headers or None,
    )
    return str(getattr(resp.content[0], "text", ""))


def _canon_call_local(user_msg: str) -> str:
    import requests

    if not LOCAL_LLM_BASE_URL or not LOCAL_LLM_MODEL:
        raise RuntimeError(
            "canon backend=local but LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL are not set"
        )
    payload: dict[str, Any] = {
        "model": LOCAL_LLM_MODEL,
        "messages": [
            {"role": "system", "content": CANON_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "max_tokens": max(8000, LOCAL_LLM_MAX_TOKENS),
    }
    if LOCAL_LLM_DISABLE_THINKING:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    r = requests.post(
        f"{LOCAL_LLM_BASE_URL}/chat/completions",
        json=payload,
        timeout=LOCAL_LLM_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse_canon_mapping(raw: str) -> dict:
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        t = m.group(0)
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid canonicalization JSON: {e}") from e
    out = {"concepts": {}, "tags": {}, "notes": str(data.get("notes", ""))[:600]}
    for k in ("concepts", "tags"):
        raw_map = data.get(k, {}) or {}
        if not isinstance(raw_map, dict):
            continue
        for src, dst in raw_map.items():
            src_s = str(src).strip()
            dst_s = str(dst).strip()
            if src_s and dst_s:
                out[k][src_s] = dst_s
    return out


def _apply_canon_to_vault(
    lesson_files: list[Path], mapping: dict, dry_run: bool
) -> dict[str, int]:
    """Rewrite frontmatter of every lesson file using the mapping.

    Returns a stats dict. Preserves body_sha so classification cache still hits.
    """
    cmap: dict[str, str] = mapping.get("concepts", {})
    tmap: dict[str, str] = mapping.get("tags", {})
    stats = {"files_changed": 0, "concept_replacements": 0, "tag_replacements": 0}
    for p in lesson_files:
        text = p.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if not fm:
            continue
        before_concepts = list(fm.get("concepts", []) or [])
        before_tags = list(fm.get("tags", []) or [])
        after_concepts: list[str] = []
        after_tags: list[str] = []
        for c in before_concepts:
            c_s = str(c).strip()
            mapped = cmap.get(c_s, c_s)
            if mapped != c_s:
                stats["concept_replacements"] += 1
            if mapped and mapped not in after_concepts:
                after_concepts.append(mapped)
        for t in before_tags:
            t_s = str(t).strip()
            mapped = tmap.get(t_s, t_s)
            if mapped != t_s:
                stats["tag_replacements"] += 1
            if mapped and mapped not in after_tags:
                after_tags.append(mapped)
        if after_concepts == before_concepts and after_tags == before_tags:
            continue
        fm["concepts"] = after_concepts
        fm["tags"] = after_tags
        new_text = dump_frontmatter(fm) + body
        if dry_run:
            stats["files_changed"] += 1
            continue
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(p)
        stats["files_changed"] += 1
    return stats


def _canon_commit(
    vault_root: Path, backend_label: str, stats: dict, mapping_path: Path
) -> None:
    if not (vault_root / ".git").exists():
        print("  vault is not a git repo — skipping auto-commit (changes are on disk)")
        return
    subprocess.run(["git", "add", "-A"], cwd=vault_root, check=True)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=vault_root)
    if r.returncode == 0:
        print("  no changes to commit")
        return
    msg = (
        f"vault: canonicalize tags + concepts ({backend_label})\n"
        f"\n"
        f"files changed: {stats['files_changed']}\n"
        f"concept replacements: {stats['concept_replacements']}\n"
        f"tag replacements: {stats['tag_replacements']}\n"
        f"\n"
        f"mapping: {mapping_path.name}"
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=vault@local",
            "-c",
            "user.name=vault",
            "commit",
            "-q",
            "-m",
            msg,
        ],
        cwd=vault_root,
        check=True,
    )
    print("  committed canonicalization snapshot")


def run_canonicalize(vault_root: Path, backend: str, apply: bool) -> int:
    print(
        f"canonicalize: vault={vault_root}  backend={backend}  model={CANON_MODEL}  apply={apply}"
    )
    concept_paths, concept_counts, tag_paths, tag_counts, lesson_files = (
        _collect_vault_inventory(vault_root)
    )
    print(f"  scanned {len(lesson_files)} lessons")
    print(
        f"  unique concepts: {len(concept_counts)} (total occurrences: {sum(concept_counts.values())})"
    )
    print(
        f"  unique tags:     {len(tag_counts)} (total occurrences: {sum(tag_counts.values())})"
    )
    if not lesson_files:
        print(
            "  no lessons found with vault_schema frontmatter; nothing to canonicalize",
            file=sys.stderr,
        )
        return 1

    user_msg = _build_canon_prompt(concept_counts, tag_counts)
    print(f"  prompt size: {len(user_msg)} chars")

    print(f"  calling {backend} ({CANON_MODEL}) ...")
    if backend == "sdk":
        raw = _canon_call_sdk(user_msg)
    elif backend == "api":
        raw = _canon_call_api(user_msg)
    elif backend == "local":
        raw = _canon_call_local(user_msg)
    else:
        print(f"  unknown canon backend: {backend}", file=sys.stderr)
        return 2
    print(f"  response: {len(raw)} chars")

    try:
        mapping = _parse_canon_mapping(raw)
    except Exception as e:
        print(f"  failed to parse mapping: {e}", file=sys.stderr)
        # dump raw so the user can inspect
        dump = vault_root / ".vault-notes" / "canonicalize-raw.txt"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(raw, encoding="utf-8")
        print(f"  raw response saved to {dump}", file=sys.stderr)
        return 3

    # Report merges (src -> dst where dst != src)
    concept_merges = {s: d for s, d in mapping["concepts"].items() if s != d}
    tag_merges = {s: d for s, d in mapping["tags"].items() if s != d}
    print(f"  proposed merges: {len(concept_merges)} concepts, {len(tag_merges)} tags")
    if mapping.get("notes"):
        print(f"  model notes: {mapping['notes']}")

    # Always write the mapping artifact alongside the vault
    notes_dir = vault_root / ".vault-notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = notes_dir / "canonicalize-mapping.json"
    mapping_path.write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  wrote mapping: {mapping_path}")

    if not apply:
        # Dry-run: report top merges and stop.
        sample = list(concept_merges.items())[:12]
        print("\n  sample concept merges (dry-run):")
        for s, d in sample:
            print(f"    {s!r} -> {d!r}")
        print("  run again with --canon-apply to rewrite frontmatters and commit.")
        return 0

    stats = _apply_canon_to_vault(lesson_files, mapping, dry_run=False)
    print(
        f"  applied: files_changed={stats['files_changed']} concept_replacements={stats['concept_replacements']} tag_replacements={stats['tag_replacements']}"
    )
    _canon_commit(vault_root, f"{backend}/{CANON_MODEL}", stats, mapping_path)
    return 0


# ---------- main ----------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="e.g. downloads/makerschool")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--force", action="store_true", help="re-call Haiku even if body_sha matches"
    )
    ap.add_argument(
        "--no-ai", action="store_true", help="skip Haiku (structural pass only)"
    )
    ap.add_argument(
        "--concurrency", type=int, default=4, help="Concurrent sdk/api calls"
    )
    ap.add_argument(
        "--local-concurrency",
        type=int,
        default=2,
        help="Concurrent calls to the local LLM server (keep low on single-GPU hosts)",
    )
    ap.add_argument(
        "--backend",
        choices=["auto", "sdk", "api", "local", "codex", "hybrid"],
        default="auto",
        help="auto = api if ANTHROPIC_API_KEY set, else sdk. "
        "sdk uses Claude CLI and falls back via AI_FALLBACK_BACKENDS (default: codex). "
        "codex = Codex CLI fallback. local = OpenAI-compatible server (LOCAL_LLM_* env). "
        "hybrid = Haiku for technical paths (--haiku-paths), local for the rest.",
    )
    ap.add_argument(
        "--haiku-paths",
        default=",".join(DEFAULT_HAIKU_PATH_GLOBS),
        help="Comma-separated fnmatch globs matched against course/module/title; used only by --backend hybrid",
    )
    ap.add_argument("--git-init", action="store_true")
    ap.add_argument(
        "--canonicalize",
        action="store_true",
        help="Run a one-shot dedup pass on the existing vault — no re-classification. "
        "Sends the full tag+concept inventory to CANON_MODEL and applies the resulting mapping.",
    )
    ap.add_argument(
        "--canon-apply",
        action="store_true",
        help="With --canonicalize: actually rewrite frontmatters + git commit. Without it: dry-run, print sample + write mapping JSON.",
    )
    ap.add_argument(
        "--canon-backend",
        choices=["sdk", "api", "local"],
        default=None,
        help="Override CANON_BACKEND env for this run.",
    )
    ap.add_argument(
        "--course-from-section",
        dest="course_from_section",
        action="store_true",
        default=True,
        help="Derive the lesson `course:` frontmatter field from the section folder "
        "name (immediate child of vault_root). Default ON. Required when the vault "
        "is flat (Month 1/, Month 2/, ... directly under vault_root).",
    )
    ap.add_argument(
        "--no-course-from-section",
        dest="course_from_section",
        action="store_false",
        help="Legacy mode: hard-code `course:` to vault_root folder name "
        "(e.g. 'Maker School') for vaults still nested under one top-level wrapper.",
    )
    args = ap.parse_args()

    vault_root = args.root.resolve()
    if not vault_root.is_dir():
        print(f"not a dir: {vault_root}", file=sys.stderr)
        return 1

    # Vault duplication sanity check: if vault_root contains a folder named the
    # same as itself (e.g. /makerschool/makerschool/), abort. This is the layout-fork
    # bug from the 2026-05 incident — refuse to operate on a duplicated tree.
    nested_dup = vault_root / vault_root.name
    if nested_dup.is_dir():
        print(
            f"❌ Vault duplication detected: {nested_dup}/ exists.\n"
            f"   This indicates the layout-fork bug. Consolidate before re-running.\n"
            f"   See .planning/ for remediation guidance, or exit and inspect manually.",
            file=sys.stderr,
        )
        return 2

    if args.canonicalize:
        canon_backend = args.canon_backend or CANON_BACKEND
        return run_canonicalize(vault_root, canon_backend, apply=args.canon_apply)

    # Resolve backend
    if args.no_ai:
        backend = "none"
    elif args.backend == "auto":
        backend = "api" if os.environ.get("ANTHROPIC_API_KEY") else "sdk"
    else:
        backend = args.backend
    print(f"backend: {backend}")
    haiku_globs = tuple(g.strip() for g in args.haiku_paths.split(",") if g.strip())

    print(f"scanning {vault_root} ... (course_from_section={args.course_from_section})")
    lessons = scan_lessons(vault_root, course_from_section=args.course_from_section)
    print(f"  found {len(lessons)} lessons")
    if args.limit:
        lessons = lessons[: args.limit]
        print(f"  limited to {len(lessons)}")

    link_neighbors(lessons)

    # Phase 1: enrich (AI) + compute new_path
    print("enriching transcripts ...")
    import asyncio

    asyncio.run(
        run_async_enrichment(
            lessons,
            args.force,
            mode=backend,
            sdk_concurrency=args.concurrency,
            local_concurrency=args.local_concurrency,
            haiku_globs=haiku_globs,
        )
    )
    for L in lessons:
        _finalize_filename(L)

    # Phase 2: MOCs (computes concept paths used by lesson rendering)
    print("writing MOCs ...")
    concept_paths = write_mocs(lessons, vault_root, args.dry_run)

    # Phase 3: render + rename lessons
    print("rendering lessons ...")
    renamed = 0
    for L in lessons:
        new_path, content = render_lesson(L, vault_root, concept_paths)
        if args.dry_run:
            continue
        new_path.parent.mkdir(parents=True, exist_ok=True)
        # write content atomically
        tmp = new_path.with_suffix(new_path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(new_path)
        # delete the old transcript.md if it differs from new_path
        if (
            L.transcript_path
            and L.transcript_path.exists()
            and L.transcript_path.resolve() != new_path.resolve()
        ):
            L.transcript_path.unlink()
            renamed += 1
    print(f"  rendered {len(lessons)} lessons, renamed {renamed} originals")

    if args.git_init and not args.dry_run:
        print("initializing git ...")
        git_init(vault_root, args.dry_run)

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
