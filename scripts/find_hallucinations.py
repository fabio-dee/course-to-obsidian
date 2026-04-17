#!/usr/bin/env python3
"""Scan transcript.md files under a root dir for Whisper-style repetition-loop hallucinations.

Flags a transcript if it contains:
  * a full sentence (>=20 chars) repeated >=3 times consecutively, OR
  * a 4-8 word phrase repeated >=10 times consecutively.

Prints a severity-sorted report and, with --list-paths, emits newline-separated
video paths suitable for piping into: xargs -I{} transcribe_videos.py {} --force.

Usage:
    python scripts/find_hallucinations.py downloads/makerschool
    python scripts/find_hallucinations.py downloads/makerschool --list-paths > /tmp/redo.txt
    python scripts/find_hallucinations.py downloads/makerschool --min-severity 100
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
WORD_SPLIT = re.compile(r"\s+")


def load_body(md: Path) -> str:
    text = md.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\n.*?\n---\n(.*)", text, re.DOTALL)
    return m.group(1) if m else text


def longest_sentence_run(body: str, min_len: int = 20) -> tuple[int, str]:
    """Longest run of consecutive identical sentences (length >= min_len)."""
    sents = [s.strip() for s in SENTENCE_SPLIT.split(body)]
    best_run, best_sent = 1, ""
    run, prev = 1, ""
    for s in sents:
        if len(s) < min_len:
            run, prev = 1, ""
            continue
        if s == prev:
            run += 1
            if run > best_run:
                best_run, best_sent = run, s
        else:
            run, prev = 1, s
    return best_run, best_sent


def longest_phrase_run(
    body: str, min_words: int = 4, max_words: int = 8, min_run: int = 10
) -> tuple[int, str]:
    """Longest run of consecutive identical N-grams, for N in [min_words, max_words]."""
    words = WORD_SPLIT.split(body.strip())
    best_run, best_phrase = 0, ""
    for n in range(min_words, max_words + 1):
        if len(words) < n * min_run:
            continue
        i = 0
        while i < len(words) - n:
            j = i + n
            run = 1
            while j + n <= len(words) and words[j : j + n] == words[i : i + n]:
                run += 1
                j += n
            if run >= min_run and run > best_run:
                best_run = run
                best_phrase = " ".join(words[i : i + n])
            i = j if run > 1 else i + 1
    return best_run, best_phrase


def severity(sent_run: int, sent_text: str, phrase_run: int, phrase_text: str) -> int:
    """Heuristic score: repeated chars, roughly the noise volume injected."""
    s_score = sent_run * len(sent_text) if sent_run >= 3 else 0
    p_score = phrase_run * len(phrase_text) if phrase_run >= 10 else 0
    return max(s_score, p_score)


def sibling_video(md: Path, filename: str = "video.mp4") -> Path | None:
    v = md.parent / filename
    return v if v.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--output", default="transcript.md", help="Transcript filename (default: transcript.md)")
    ap.add_argument("--video", default="video.mp4", help="Video filename (default: video.mp4)")
    ap.add_argument("--min-severity", type=int, default=60, help="Report threshold (default: 60)")
    ap.add_argument("--list-paths", action="store_true", help="Emit only flagged video paths, one per line")
    args = ap.parse_args()

    transcripts = sorted(args.root.rglob(args.output))
    flagged: list[tuple[int, Path, int, str, int, str]] = []
    for md in transcripts:
        body = load_body(md)
        sr, ss = longest_sentence_run(body)
        pr, ps = longest_phrase_run(body)
        sev = severity(sr, ss, pr, ps)
        if sev >= args.min_severity:
            flagged.append((sev, md, sr, ss, pr, ps))

    flagged.sort(key=lambda x: -x[0])

    if args.list_paths:
        for _, md, *_ in flagged:
            vid = sibling_video(md, args.video)
            if vid:
                print(vid)
        return 0

    print(f"Scanned {len(transcripts)} transcripts. Flagged {len(flagged)} (severity >= {args.min_severity}).\n")
    if not flagged:
        return 0

    print(f"{'sev':>6} {'sent×':>6} {'phr×':>5}  path | worst pattern")
    print("-" * 100)
    for sev, md, sr, ss, pr, ps in flagged:
        rel = md.relative_to(args.root)
        if sr >= 3 and sr * len(ss) >= pr * len(ps):
            sample = f'"{ss[:70]}"'
            kind = f"{sr}× sent"
        else:
            sample = f'"{ps[:70]}"'
            kind = f"{pr}× phr"
        print(f"{sev:6} {sr:>6} {pr:>5}  {str(rel)[:60]:60} | {kind}: {sample}")

    print(f"\nHint: re-transcribe with --force:")
    print(f"  python {Path(__file__).name} {args.root} --list-paths | while read v; do \\")
    print(f"    python scripts/transcribe_videos.py \"$(dirname \"$v\")\" --limit 1 --force; done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
