# USAGE — operator runbook

Practical, copy-pasteable commands for the two-repo Skool → Obsidian pipeline.
For *what this is* and *why two repos*, see the wrapper `README.md`. For
internals, see each repo's own `README.md`.

All paths assume you are inside the wrapper folder
(`0_Skool_Course_Download/`). `<slug>` is the Skool community slug as it
appears in the URL (e.g. `maker-school`, `startupempire`). Local download
folders may use a different slug (e.g. `downloads/makerschool`) — pass that
to `-o`.

---

## 0. One-time setup

```bash
# Login to Skool (saves storage_state.json under skool-downloader/.auth/)
cd skool-downloader
npm install
npx playwright install chromium
npm run login
cd ..

# Shared Python venv (Python 3.12)
cd skool-downloader
python3.12 -m venv .venv-transcribe-p312
.venv-transcribe-p312/bin/pip install -r ../course-to-obsidian/scripts/transcribe_videos.requirements.txt
.venv-transcribe-p312/bin/pip install anthropic pyyaml markdownify beautifulsoup4 claude-agent-sdk
cd ..
```

---

## 1. Download / scrape

### First-time scrape of a course

```bash
cd skool-downloader
npx tsx src/cli.ts https://www.skool.com/<slug>/classroom/<course-id>
```

### First-time scrape of an entire community (all courses)

```bash
cd skool-downloader
npx tsx src/cli.ts https://www.skool.com/<slug>/classroom
```

### Bootstrap fingerprints on an existing vault (one-time, recommended before first `--update`)

After updating to the multi-signal fingerprint scheme (Phase 5+), any vault scraped before this rolled out should be re-fingerprinted once. This is local-only — no Playwright, no network — and stamps `fp_schema: 2` so subsequent `--update` runs skip the slow lazy-rebuild path.

```bash
cd skool-downloader
npx tsx src/cli.ts --refingerprint -o downloads/<local-folder>
# Already-current lessons are skipped automatically.
# Use --force-refingerprint to recompute everything (after a schema bump).
```

### Check for new / updated lessons (re-download only what changed)

```bash
cd skool-downloader
npx tsx src/cli.ts https://www.skool.com/<slug>/classroom --update -o downloads/<local-folder>

# If >30% of an existing vault is suddenly flagged NEW, the run aborts with
# a report at <vault>/.update-aborted-<ts>.json. To bypass:
#   add --force-update
```

Examples:

```bash
# Maker School (slug is `makerschool`, no hyphen — verify by opening the
# classroom in your browser; the path segment after skool.com/ is the slug)
npx tsx src/cli.ts https://www.skool.com/makerschool/classroom --update -o downloads/makerschool

# Startup Empire
npx tsx src/cli.ts https://www.skool.com/startupempire/classroom --update -o downloads/startupempire
```

### See what changed in the last run(s)

```bash
cd skool-downloader
npx tsx src/cli.ts log downloads/<local-folder> --latest        # last run
npx tsx src/cli.ts log downloads/<local-folder> --last 5        # last 5 runs
npx tsx src/cli.ts log downloads/<local-folder> --since 7d      # last 7 days
npx tsx src/cli.ts log downloads/<local-folder> --json          # machine-readable
```

---

## 2. Full end-to-end pipeline

Wraps download → transcribe → vault build → git commit. Idempotent; safe to re-run.

```bash
course-to-obsidian/scripts/full_pipeline.sh https://www.skool.com/<slug>/classroom/<course-id>
```

Skip-flags (set to `1` to skip a stage):

| Var | Effect |
|---|---|
| `SKOOL_SKIP_DOWNLOAD` | skip the scrape step |
| `SKOOL_SKIP_TRANSCRIBE` | skip transcription |
| `SKOOL_SKIP_VAULT` | skip vault build / re-tagging |
| `SKOOL_SKIP_GIT` | skip auto-commit inside the vault |

Other knobs:

| Var | Default | Notes |
|---|---|---|
| `SKOOL_BACKEND` | `sdk` | `sdk` (Claude CLI / Max sub) or `api` (needs `ANTHROPIC_API_KEY`) |
| `SKOOL_VENV` | `skool-downloader/.venv-transcribe-p312` | absolute path |
| `SKOOL_DOWNLOADER_DIR` | `../skool-downloader` | sibling resolution |

### Common recipes

```bash
# Re-tag the existing vault after prompt/script changes (idempotent via body_sha)
SKOOL_SKIP_DOWNLOAD=1 SKOOL_SKIP_TRANSCRIBE=1 SKOOL_SKIP_GIT=1 \
  course-to-obsidian/scripts/full_pipeline.sh <url>

# Just transcribe newly downloaded videos
SKOOL_SKIP_DOWNLOAD=1 SKOOL_SKIP_VAULT=1 SKOOL_SKIP_GIT=1 \
  course-to-obsidian/scripts/full_pipeline.sh <url>

# Force re-classification of every lesson (~$1–2 in Haiku for a ~600-lesson course)
skool-downloader/.venv-transcribe-p312/bin/python \
  course-to-obsidian/scripts/build_obsidian_vault.py \
  skool-downloader/downloads/<local-folder> --backend sdk --force
```

---

## 3. Vault maintenance

### Inspect classification stats / failures

```bash
skool-downloader/.venv-transcribe-p312/bin/python \
  course-to-obsidian/scripts/transcript_stats.py \
  skool-downloader/downloads/<local-folder> \
  --failures skool-downloader/downloads/<local-folder>/.vault-notes/ai-failures.md
```

### Vault-wide tag + concept dedup (canonicalize)

```bash
skool-downloader/.venv-transcribe-p312/bin/python \
  course-to-obsidian/scripts/build_obsidian_vault.py \
  skool-downloader/downloads/<local-folder> --canonicalize
```

---

## 4. Video re-encoding (HEVC / H.265)

Drops vault size dramatically; outputs play natively on macOS (hvc1 tag).
Originals are **never** deleted by these scripts — verify a batch before
deleting `.mp4` originals manually.

### Step A — classify video content type (read-only)

Writes `<vault>/.vault-notes/video-types.json`. Drives per-content CRF + resolution.

```bash
skool-downloader/.venv-transcribe-p312/bin/python \
  course-to-obsidian/scripts/classify_video_type.py \
  skool-downloader/downloads/<local-folder>
```

### Step B — encode

Convenience launcher (runs against both vaults):

```bash
course-to-obsidian/scripts/reencode_all.sh                # concurrency=2, preset=medium
course-to-obsidian/scripts/reencode_all.sh 4              # AFK mode
course-to-obsidian/scripts/reencode_all.sh 6 slow         # slower preset, slightly smaller files
```

Or directly on one vault:

```bash
skool-downloader/.venv-transcribe-p312/bin/python \
  course-to-obsidian/scripts/reencode_videos.py \
  skool-downloader/downloads/<local-folder> --concurrency 2 --preset medium
```

Idempotent — Ctrl-C and re-run is safe; sidecars (`*.hevc.json`) skip already-done videos.

### Step C — graceful drain (change concurrency without losing in-flight encodes)

```bash
course-to-obsidian/scripts/reencode_drain.sh start    # stop picking new tasks
course-to-obsidian/scripts/reencode_drain.sh status   # wait until "running ffmpegs: 0"
course-to-obsidian/scripts/reencode_drain.sh stop     # remove sentinel
course-to-obsidian/scripts/reencode_all.sh 4          # restart at new concurrency
```

---

## 5. Git workflows

### `skool-downloader/` — push to both remotes (public + private mirror)

```bash
cd skool-downloader
git pushboth main          # → origin-public then origin-private
```

### Cherry-pick a specific upstream fix

```bash
cd skool-downloader
git fetch upstream
git log upstream/main --oneline --not main         # what's new upstream
git cherry-pick <sha>
git pushboth main
```

### Start a feature branch

```bash
cd skool-downloader      # or course-to-obsidian
git switch -c feat/<name> main
# …develop, commit…
git switch main
git merge --no-ff feat/<name>
git pushboth main        # skool-downloader only; course-to-obsidian uses plain `git push`
```

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `auth required` on scrape | session expired | `cd skool-downloader && npm run login` |
| Haiku calls fail with auth error in `--backend api` | missing `ANTHROPIC_API_KEY` | export it, or switch to `--backend sdk` |
| Re-encode skips a video forever | sidecar fingerprint pinned to old policy | delete `<video>.hevc.json` and re-run |
| Vault git step errors | nested `.git` at `downloads/<slug>/.git` is missing or detached | `cd downloads/<slug> && git status` to inspect |
| `tsx: command not found` | deps not installed | `cd skool-downloader && npm install` |
| Update mode finds nothing changed | normal — Skool fingerprints are stable until edited | check with `npx tsx src/cli.ts log <dir> --latest` |

---

## 7. File layout reference

```
skool-downloader/downloads/<slug>/
├── .group-log.json                  # run history + lesson fingerprints
├── .vault-notes/
│   ├── video-types.json             # classify_video_type.py output
│   └── ai-failures.md               # tagging failures (optional)
├── _original_.obsidian/             # preserved Obsidian config
├── <Module>/<Lesson>/
│   ├── index.html                   # offline viewer
│   ├── lesson.json                  # raw scrape payload
│   ├── video.mp4                    # original
│   ├── video.hevc.mp4               # re-encoded (after step 4B)
│   ├── video.hevc.json              # re-encode sidecar (fingerprint)
│   ├── transcript.md                # parakeet output
│   └── resources/
└── NN. <Lesson Title>.md            # Obsidian note (frontmatter + body)
```

Secrets (never commit): `.auth/`, `cookies.txt`, `storage_state.json`, `.env`, `ANTHROPIC_API_KEY`.
