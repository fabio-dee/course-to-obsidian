#!/usr/bin/env python3
"""
vault_integrity_check.py — read-only validator for an Obsidian course vault.

Usage:
    python vault_integrity_check.py <vault-root>

Exit codes:
    0 = OK         (no issues)
    1 = WARN       (non-fatal issues found)
    2 = FAIL       (fatal issues found — pipeline should refuse to commit)

Checks:
  1. Unique lesson_id           — every lesson.json has lessonId; no duplicates (FAIL on dupes)
  2. Frontmatter schema         — every lesson .md has vault_schema:1 + required fields
  3. Orphan concept stubs       — Concepts/*.md referenced by ≥1 lesson frontmatter
  4. MOC wikilinks resolve      — root MOC + per-section _<section>.md links resolve
  5. Body wikilinks to concepts — sample lessons; flag links to canonicalized variants
  6. Vault duplication sanity   — FAIL if <vault>/<vault.name>/ nested duplicate exists
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from collections import defaultdict

# ---------- output helpers ----------
USE_COLOR = sys.stdout.isatty()
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s
def green(s):  return _c("32", s)
def yellow(s): return _c("33", s)
def red(s):    return _c("31", s)
def bold(s):   return _c("1",  s)

OK_COUNT = 0
WARN_COUNT = 0
FAIL_COUNT = 0

def ok(check: str, msg: str = ""):
    global OK_COUNT; OK_COUNT += 1
    print(f"  {green('OK  ')} {check}" + (f" — {msg}" if msg else ""))

def warn(check: str, msg: str):
    global WARN_COUNT; WARN_COUNT += 1
    print(f"  {yellow('WARN')} {check} — {msg}")

def fail(check: str, msg: str):
    global FAIL_COUNT; FAIL_COUNT += 1
    print(f"  {red('FAIL')} {check} — {msg}")

# ---------- frontmatter parser (no yaml dep needed) ----------
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

def parse_frontmatter(text: str) -> dict:
    """Tiny YAML-ish parser. Handles `key: value`, `key:` followed by `- item` lists.
    Sufficient for our pipeline-owned frontmatter."""
    m = FM_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    out: dict = {}
    cur_key = None
    cur_list: list | None = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("- ") and cur_list is not None:
            cur_list.append(line[2:].strip().strip('"').strip("'"))
            continue
        if ":" in line and not line.startswith(" "):
            if cur_key is not None and cur_list is not None:
                out[cur_key] = cur_list
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if not val:
                cur_key = key
                cur_list = []
            else:
                out[key] = val.strip('"').strip("'")
                cur_key = None
                cur_list = None
    if cur_key is not None and cur_list is not None:
        out[cur_key] = cur_list
    return out

# ---------- checks ----------
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")

def iter_lesson_md(vault: Path):
    """Lesson .md = a .md file that has a sibling lesson.json."""
    for j in vault.rglob("lesson.json"):
        # skip nested duplicate vault if any
        if any(p.name.startswith(".") for p in j.relative_to(vault).parents):
            continue
        for md in j.parent.glob("*.md"):
            if md.name.startswith("_"):  # skip MOCs
                continue
            yield md, j

def check_unique_lesson_id(vault: Path):
    print(bold("\n[1] Unique lesson_id"))
    seen: dict[str, list[Path]] = defaultdict(list)
    missing = 0
    total = 0
    for j in vault.rglob("lesson.json"):
        total += 1
        try:
            data = json.loads(j.read_text(encoding="utf-8"))
        except Exception as e:
            fail("lesson.json parse", f"{j}: {e}"); continue
        lid = data.get("lessonId") or data.get("lesson_id")
        if not lid:
            missing += 1
            continue
        seen[lid].append(j)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if dupes:
        for lid, paths in list(dupes.items())[:5]:
            fail("duplicate lessonId", f"{lid}: {len(paths)} occurrences")
        if len(dupes) > 5:
            fail("duplicate lessonId", f"... +{len(dupes)-5} more dupes")
    if missing:
        warn("missing lessonId", f"{missing}/{total} lesson.json files lack lessonId")
    if not dupes and not missing:
        ok("lesson_id unique", f"{total} lesson.json checked")

def check_frontmatter_schema(vault: Path):
    print(bold("\n[2] Frontmatter schema"))
    REQUIRED = ("lesson_id", "body_sha", "tags", "concepts")
    no_schema = []
    missing_field: dict[str, int] = defaultdict(int)
    total = 0
    for md, _ in iter_lesson_md(vault):
        total += 1
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = parse_frontmatter(text)
        if str(fm.get("vault_schema", "")) != "1":
            no_schema.append(md)
            continue
        for f in REQUIRED:
            if f not in fm:
                missing_field[f] += 1
    if no_schema:
        for p in no_schema[:5]:
            fail("vault_schema missing", str(p.relative_to(vault)))
        if len(no_schema) > 5:
            fail("vault_schema missing", f"... +{len(no_schema)-5} more")
    for f, n in missing_field.items():
        warn(f"missing field `{f}`", f"{n} lessons")
    if not no_schema and not missing_field:
        ok("schema valid", f"{total} lessons")

def check_orphan_concepts(vault: Path):
    print(bold("\n[3] Orphan concept stubs"))
    concepts_dir = vault / "Concepts"
    if not concepts_dir.is_dir():
        warn("no Concepts/ folder", "skip")
        return
    # Concept names in fm are raw (e.g. "The 80/20 Principle"); stub filenames
    # are produced via build_obsidian_vault.safe_filename which strips path-illegal
    # chars (/, \, :, *, ?, ", <, >, |). Normalize fm names the same way before
    # comparing — otherwise legit stubs look orphaned (false positive).
    illegal = re.compile(r'[\\/:*?"<>|]')
    def _normalize(name: str) -> str:
        return illegal.sub("", str(name)).strip().lower()

    referenced: set[str] = set()
    for md, _ in iter_lesson_md(vault):
        try:
            fm = parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in fm.get("concepts", []) or []:
            referenced.add(_normalize(c))
    stubs = list(concepts_dir.glob("*.md"))
    orphans = [s for s in stubs if _normalize(s.stem) not in referenced]
    if orphans:
        warn("orphan concept stubs", f"{len(orphans)}/{len(stubs)} stubs unreferenced (expected after canonicalization; consider cleanup)")
    else:
        ok("concept stubs referenced", f"{len(stubs)} stubs all referenced")

def check_moc_wikilinks(vault: Path):
    print(bold("\n[4] MOC wikilinks resolve"))
    mocs: list[Path] = []
    # root MOC: any top-level .md (heuristic: title-cased, no underscore prefix, not README)
    for md in vault.glob("*.md"):
        if md.name.startswith("_"):
            mocs.append(md)
        elif md.stem.lower() not in {"readme", "claude"}:
            mocs.append(md)
    # per-section _<section>.md MOCs
    for sec in vault.iterdir():
        if not sec.is_dir() or sec.name.startswith("."):
            continue
        for md in sec.glob("_*.md"):
            mocs.append(md)
    broken = 0
    checked = 0
    for moc in mocs:
        try:
            text = moc.read_text(encoding="utf-8")
        except Exception:
            continue
        for link in WIKILINK_RE.findall(text):
            checked += 1
            target = link.strip()
            # Resolve: try as path-relative-to-vault, then by basename in vault
            candidates = [vault / f"{target}.md", vault / target]
            if "/" not in target:
                candidates += list(vault.rglob(f"{target}.md"))
            if not any(c.exists() for c in candidates):
                broken += 1
    if broken:
        warn("broken MOC wikilinks", f"{broken}/{checked} links unresolved across {len(mocs)} MOCs")
    else:
        ok("MOC wikilinks resolve", f"{checked} links across {len(mocs)} MOCs")

def check_body_wikilinks_to_canonicalized(vault: Path):
    print(bold("\n[5] Body wikilinks to canonicalized concepts"))
    mapping_path = vault / ".vault-notes" / "canonicalize-mapping.json"
    if not mapping_path.exists():
        warn("no canonicalize-mapping.json", "skip")
        return
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except Exception as e:
        warn("mapping parse", str(e)); return
    # Mapping keys are variants → canonical. Exclude identity entries (k == v):
    # those keys ARE the canonical, so a body wikilink to them is not a stale variant.
    concept_variants: set[str] = set()
    cmap = mapping.get("concepts", mapping) if isinstance(mapping, dict) else {}
    if isinstance(cmap, dict):
        concept_variants = {
            str(k).strip().lower()
            for k, v in cmap.items()
            if str(k).strip().lower() != str(v).strip().lower()
        }
    if not concept_variants:
        ok("no canonicalized concepts to check")
        return
    sample = list(iter_lesson_md(vault))[:50]  # sample
    hits = 0
    for md, _ in sample:
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        # strip frontmatter
        body = FM_RE.sub("", text, count=1)
        for link in WIKILINK_RE.findall(body):
            tgt = link.strip().lower()
            if tgt.startswith("concepts/"):
                tgt = tgt.split("/", 1)[1]
            if tgt in concept_variants:
                hits += 1
                break
    if hits:
        warn("body wikilinks reference canonicalized variants",
             f"{hits}/{len(sample)} sampled lessons (note: --canon-apply does not rewrite body wikilinks; only frontmatter)")
    else:
        ok("body wikilinks clean (sample)", f"{len(sample)} lessons sampled")

def check_vault_duplication(vault: Path):
    print(bold("\n[6] Vault duplication sanity"))
    nested = vault / vault.name
    if nested.is_dir():
        fail("nested duplicate vault", f"{nested} exists — likely layout-fork bug")
    else:
        ok("no nested duplicate vault")

# ---------- main ----------
def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <vault-root>", file=sys.stderr); sys.exit(2)
    vault = Path(sys.argv[1]).resolve()
    if not vault.is_dir():
        print(f"error: {vault} is not a directory", file=sys.stderr); sys.exit(2)
    print(bold(f"vault_integrity_check: {vault}"))
    check_unique_lesson_id(vault)
    check_frontmatter_schema(vault)
    check_orphan_concepts(vault)
    check_moc_wikilinks(vault)
    check_body_wikilinks_to_canonicalized(vault)
    check_vault_duplication(vault)
    print()
    print(bold(f"summary: OK={OK_COUNT} WARN={WARN_COUNT} FAIL={FAIL_COUNT}"))
    if FAIL_COUNT:
        sys.exit(2)
    if WARN_COUNT:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
