# course-to-obsidian

> Turn an online course scrape into an Obsidian-ready living library — semantic tagging via Claude Haiku 4.5, wikilinks, and Maps of Content. Currently supports Skool course scrapes produced by [balmasi/skool-downloader](https://github.com/balmasi/skool-downloader).

## What it does

`course-to-obsidian` takes the raw download output from a course scraper (a folder of HTML lesson files plus optional video assets) and transforms it into a fully interlinked Obsidian vault. Each lesson becomes a structured Markdown note with frontmatter (title, module, tags, summary), a `## Lesson notes` section from the HTML body, an optional `## Transcript` section if a video was transcribed, and a `## Resources` section for any linked assets. Beyond individual lessons, the pipeline generates module-level and course-level Maps of Content (MOCs) that link every lesson in sequence, plus a root index MOC. A dedicated `Concepts/` folder holds cross-lesson concept stubs wikilinked from every lesson that mentions them — making the vault a semantic knowledge base, not just a flat note dump.

## Requirements

- Python 3.12+
- ffmpeg — `brew install ffmpeg` (required for video transcription)
- **For vault building (stage 2):** either
  - `claude` CLI with a Claude Max subscription (for `--backend sdk`, uses the Agent SDK)
  - `ANTHROPIC_API_KEY` environment variable (for `--backend api`, faster via prompt caching)

## Pipeline stages

### 1. Transcribe videos

```bash
scripts/transcribe_videos.py <course-download-dir>
```

Walks the download directory looking for `video.mp4` files and writes a `transcript.md` alongside each one using [parakeet-mlx](https://huggingface.co/mlx-community) (English; pass `--backend whisper` for other languages). The script is idempotent — already-transcribed lessons are skipped.

### 2. Build the Obsidian vault

```bash
scripts/build_obsidian_vault.py <course-download-dir> --backend sdk|api
```

Processes every lesson HTML file in the download tree: renames files to `NN. Title.md` format, injects YAML frontmatter, generates module/course/root MOCs, calls Claude Haiku to produce semantic tags, concept links, and a one-paragraph summary, then injects wikilinks and a nav footer (prev/next lesson). Resource-only lessons with no body text are handled via a deterministic fallback (threshold: `SPARSE_INPUT_MIN_CHARS=300`) to avoid empty Haiku calls. The script is idempotent via a `body_sha` hash in frontmatter — re-running it skips lessons whose content has not changed.

### 3. Full end-to-end wrapper

```bash
scripts/full_pipeline.sh <skool-url> [slug]
```

Chains all stages: download (via `npm run skool` in a sibling `skool-downloader` directory) → transcribe → vault build → `git init` in the output vault. Behaviour is controlled by environment variables:

| Variable | Effect |
|---|---|
| `SKOOL_SKIP_DOWNLOAD=1` | Skip the scrape stage |
| `SKOOL_SKIP_TRANSCRIBE=1` | Skip video transcription |
| `SKOOL_SKIP_VAULT=1` | Skip vault build |
| `SKOOL_SKIP_GIT=1` | Skip git init |
| `SKOOL_BACKEND=sdk\|api` | Select vault-build backend (default: `sdk`) |
| `SKOOL_VENV=<path>` | Path to the Python venv to use |

## Quick start

Assuming `skool-downloader` is cloned at `../skool-downloader` with its Python venv set up:

```bash
./scripts/full_pipeline.sh https://www.skool.com/<community>/classroom/<course-id>
```

To re-run after the course has been updated (e.g. new lessons added), skip the download-heavy stages:

```bash
SKOOL_SKIP_VAULT=1 SKOOL_SKIP_GIT=1 ./scripts/full_pipeline.sh https://www.skool.com/<community>/classroom/<course-id>
```

Or to rebuild only the vault from an already-downloaded + transcribed folder:

```bash
SKOOL_SKIP_DOWNLOAD=1 SKOOL_SKIP_TRANSCRIBE=1 SKOOL_SKIP_GIT=1 \
  ./scripts/full_pipeline.sh https://www.skool.com/<community>/classroom/<course-id>
```

## Output

The resulting vault lives at `downloads/<slug>/` and has the following shape:

```
downloads/<slug>/
├── 00. Course Index.md          ← root MOC (links all modules)
├── Module 01 - Name/
│   ├── 00. Module 01 MOC.md     ← module MOC (links all lessons in order)
│   ├── 01. Lesson Title.md      ← lesson note
│   │       ## Lesson notes      ← from HTML body
│   │       ## Transcript        ← from transcript.md (if video present)
│   │       ## Resources         ← linked assets
│   └── 02. Another Lesson.md
├── Module 02 - Name/
│   └── ...
└── Concepts/
    ├── Some Concept.md          ← cross-lesson stub, wikilinked everywhere
    └── ...
```

Each lesson note's frontmatter includes `title`, `module`, `tags` (Haiku-generated), `summary` (Haiku-generated), `body_sha` (idempotency hash), and `nav` links to the previous and next lesson.

## Credits

Built on top of [balmasi/skool-downloader](https://github.com/balmasi/skool-downloader) for the scrape stage. Uses the [Anthropic Claude Agent SDK](https://github.com/anthropics/anthropic-sdk-python) for Haiku classifier calls, and [parakeet-mlx](https://huggingface.co/mlx-community) for on-device video transcription.

## License

[CC-BY-NC-4.0](LICENSE) — same as upstream skool-downloader.
