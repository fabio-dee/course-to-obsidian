#!/usr/bin/env python3
"""Phase 3 smoke tests:
  - transcript_stats.py --dedupe-by-lesson-id collapses duplicate lesson_ids
  - full_pipeline.sh growth guardrail rejects >50% size/count inflation

Run:
    python3 tests/test_phase3_stats_dedupe.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATS = REPO / "scripts" / "transcript_stats.py"
PY = sys.executable


def make_lesson(root: Path, rel: str, lesson_id: str, title: str) -> None:
    """Write a synthetic <NN>. <Title>.md with frontmatter + a Transcript section."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"lesson_id: '{lesson_id}'\n"
        f"title: \"{title}\"\n"
        "tags: []\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Transcript\n\n"
        "Some transcript text used for char counting.\n"
    )
    p.write_text(body, encoding="utf-8")


def test_dedupe_collapses_duplicate_lesson_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 3 lessons; lesson-A appears twice under different paths (the bug we're catching)
        make_lesson(root, "Mod1/01-intro/01. Intro.md",     "lesson-A", "Intro")
        make_lesson(root, "Mod2/01-intro/01. Intro Copy.md", "lesson-A", "Intro Copy")
        make_lesson(root, "Mod1/02-next/02. Next.md",       "lesson-B", "Next")

        # default mode: counts all 3 paths
        out = subprocess.run(
            [PY, str(STATS), str(root)],
            capture_output=True, text=True, check=True,
        )
        assert "total lessons: 3" in out.stdout, \
            f"default mode should count all 3 paths, got:\n{out.stdout}"
        assert "Deduped:" not in out.stdout, "no dedupe summary expected without flag"

        # dedupe mode: collapses to 2, warns once
        out = subprocess.run(
            [PY, str(STATS), str(root), "--dedupe-by-lesson-id"],
            capture_output=True, text=True, check=True,
        )
        assert "duplicate lesson_id=lesson-A" in out.stdout, \
            f"expected dupe warning, got:\n{out.stdout}"
        assert "total lessons: 2" in out.stdout, \
            f"dedupe should yield 2 unique lessons, got:\n{out.stdout}"
        assert "Deduped: 3 file paths → 2 unique lessons (1 dupes skipped)" in out.stdout, \
            f"expected dedupe summary line, got:\n{out.stdout}"

    print("✅ test_dedupe_collapses_duplicate_lesson_ids")


def test_growth_guardrail_rejects_doubling() -> None:
    """Simulate the post-build growth check from full_pipeline.sh.

    We extract the same logic (du -sk + find | wc -l + awk percentage) and run
    it against a fake vault that we deliberately double, asserting non-zero exit.
    """
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        vault.mkdir()
        for i in range(4):
            (vault / f"lesson-{i}.md").write_text("x" * 4096, encoding="utf-8")

        # Pre-baseline
        pre_size = subprocess.check_output(
            ["du", "-sk", str(vault)], text=True
        ).split()[0]
        pre_count = subprocess.check_output(
            f"find {vault} -name '*.md' -not -path '*/.git/*' | wc -l",
            shell=True, text=True,
        ).strip()

        # Inflate: duplicate every file into a sibling subdir → ~2x size + count
        dup = vault / "duplicate"
        dup.mkdir()
        for f in list(vault.glob("*.md")):
            shutil.copy2(f, dup / f.name)

        post_size = subprocess.check_output(
            ["du", "-sk", str(vault)], text=True
        ).split()[0]
        post_count = subprocess.check_output(
            f"find {vault} -name '*.md' -not -path '*/.git/*' | wc -l",
            shell=True, text=True,
        ).strip()

        # Same awk predicate as the shell script
        size_violation = subprocess.run(
            ["awk", f"-va={pre_size}", f"-vb={post_size}",
             "BEGIN { exit !((b - a) / a > 0.5) }"],
        ).returncode == 0
        count_violation = subprocess.run(
            ["awk", f"-va={pre_count}", f"-vb={post_count}",
             "BEGIN { exit !((b - a) / a > 0.5) }"],
        ).returncode == 0

        assert size_violation or count_violation, (
            f"guardrail should fire on doubling: "
            f"size {pre_size}→{post_size}, count {pre_count}→{post_count}"
        )
        # Specifically, count should have doubled
        assert int(post_count) >= 2 * int(pre_count), \
            f"sanity: post_count {post_count} should be ≥2× pre_count {pre_count}"

    print("✅ test_growth_guardrail_rejects_doubling")


def test_growth_guardrail_passes_normal_growth() -> None:
    """Adding 1 file to a 4-file vault is +25% — should NOT trip."""
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        vault.mkdir()
        for i in range(4):
            (vault / f"lesson-{i}.md").write_text("x" * 4096, encoding="utf-8")

        pre_count = subprocess.check_output(
            f"find {vault} -name '*.md' -not -path '*/.git/*' | wc -l",
            shell=True, text=True,
        ).strip()
        (vault / "lesson-new.md").write_text("x" * 4096, encoding="utf-8")
        post_count = subprocess.check_output(
            f"find {vault} -name '*.md' -not -path '*/.git/*' | wc -l",
            shell=True, text=True,
        ).strip()

        count_violation = subprocess.run(
            ["awk", f"-va={pre_count}", f"-vb={post_count}",
             "BEGIN { exit !((b - a) / a > 0.5) }"],
        ).returncode == 0
        assert not count_violation, \
            f"normal growth ({pre_count}→{post_count}) should not trip guardrail"

    print("✅ test_growth_guardrail_passes_normal_growth")


if __name__ == "__main__":
    test_dedupe_collapses_duplicate_lesson_ids()
    test_growth_guardrail_rejects_doubling()
    test_growth_guardrail_passes_normal_growth()
    print("\nAll Phase 3 tests passed.")
