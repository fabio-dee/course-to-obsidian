#!/usr/bin/env python3
"""
Measure the combined AI-input size (HTML notes + transcript) for every lesson
and flag the 16 Haiku classification failures so we can see whether size is
the common factor, and pick a safe truncation / chunking threshold empirically.

Usage:
    python scripts/transcript_stats.py downloads/makerschool \
        --failures .vault-notes/ai-failures.md
"""
from __future__ import annotations

import argparse
import re
import statistics
from pathlib import Path

TOKENS_PER_CHAR = 0.25   # rough Claude-tokenizer estimate for English prose


def extract_transcript_body(md_path: Path) -> str:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    # strip frontmatter
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5:]
    m = re.search(r"(?ms)^##\s+Transcript\s*\n+(.*?)(?=\n##\s|\n<!--\s*vault:|\Z)", text)
    return m.group(1).strip() if m else text.strip()


def extract_html_body_chars(index_html: Path) -> int:
    """Quick char count of the <div class='content'> section; no HTML→MD conversion needed."""
    try:
        html = index_html.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0
    m = re.search(r'<div class="content">(.*?)</div>', html, re.DOTALL)
    if not m:
        return 0
    # strip tags + collapse whitespace for a stable text-length estimate
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    return len(re.sub(r"\s+", " ", text).strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--failures", type=Path, help="path to ai-failures.md")
    args = ap.parse_args()

    fail_titles: set[str] = set()
    if args.failures and args.failures.exists():
        for line in args.failures.read_text().splitlines():
            if line.startswith("- "):
                fail_titles.add(line[2:].strip())

    rows: list[tuple[int, int, int, bool, str]] = []   # (combined_chars, transcript_chars, html_chars, failed, label)
    for lesson_md in args.root.rglob("[0-9][0-9]. *.md"):
        lesson_dir = lesson_md.parent
        idx = lesson_dir / "index.html"
        t_chars = len(extract_transcript_body(lesson_md))
        h_chars = extract_html_body_chars(idx) if idx.exists() else 0
        combined = t_chars + h_chars
        # match failure list (failures are the lesson titles, compared to md basename sans NN.)
        title = re.sub(r"^\d+\.\s*", "", lesson_md.stem)
        failed = title in fail_titles
        rows.append((combined, t_chars, h_chars, failed, str(lesson_md.relative_to(args.root))))

    rows.sort(reverse=True)
    combined_all = [r[0] for r in rows]
    combined_failed = [r[0] for r in rows if r[3]]
    combined_passed = [r[0] for r in rows if not r[3]]

    def pct(data, p): return int(statistics.quantiles(data, n=100)[p - 1]) if len(data) >= 100 else max(data)
    def tok(c): return int(c * TOKENS_PER_CHAR)

    print("=== combined chars (transcript + html) ===")
    print(f"  total lessons: {len(rows)}  failed: {len(combined_failed)}")
    print(f"  all:     max={max(combined_all):>7}  p99={pct(combined_all,99):>7}  p95={pct(combined_all,95):>7}  p50={pct(combined_all,50):>7}  (~tokens: {tok(max(combined_all))})")
    if combined_failed:
        print(f"  failed:  max={max(combined_failed):>7}  min={min(combined_failed):>7}  median={int(statistics.median(combined_failed)):>7}")
    if combined_passed:
        print(f"  passed:  max={max(combined_passed):>7}  p99={pct(combined_passed,99):>7}  p50={pct(combined_passed,50):>7}")

    # Top 25 + all failures
    print("\n=== top 25 by combined char count ===")
    print(f"  {'failed':<7}{'combined':>10}{'~tokens':>10}{'transcr':>10}{'html':>10}  path")
    for combined, t, h, failed, label in rows[:25]:
        flag = "FAIL" if failed else ""
        print(f"  {flag:<7}{combined:>10}{tok(combined):>10}{t:>10}{h:>10}  {label}")

    if combined_failed:
        print("\n=== all failures ===")
        print(f"  {'combined':>10}{'~tokens':>10}{'transcr':>10}{'html':>10}  path")
        for combined, t, h, failed, label in sorted([r for r in rows if r[3]], reverse=True):
            print(f"  {combined:>10}{tok(combined):>10}{t:>10}{h:>10}  {label}")


if __name__ == "__main__":
    main()
