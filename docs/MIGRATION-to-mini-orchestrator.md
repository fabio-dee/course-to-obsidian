# Migration Plan — Skool scraper/downloader → mini-orchestrator64

**Status:** planned, not executed. Prepared 2026-07-06.
**Goal:** move the whole Skool scrape → download → transcribe → re-encode → Obsidian-vault
pipeline off the Mac and onto the LAN server that already runs the Hermes instances,
with the ~30 GB of videos/resources living on the server's secondary `/data` partition.

## Locked decisions

| Decision | Choice |
|---|---|
| Transcription on server | **Local faster-whisper** (Docker, OpenAI-compatible) on the mini-orchestrator itself |
| Run model | **On-demand manual** first (SSH + `full_pipeline.sh`); leave clean seams to add a systemd timer later |
| Scope | **Stages 1–5 only** (scrape → transcribe → re-encode → vault build → git commit). Stage 6 `gbrain`/`makerschool-coach` is **out of scope** |
| Mac's role after cutover | **Server becomes sole home**; Mac decommissioned for this workload (kept as a cold backup during the transition window, then deleted) |

---

## Target end-state

```
mini-orchestrator64 (192.168.8.104), user: infra
├── ~/skool/                              # wrapper (NOT a git repo — mirrors the Mac layout)
│   ├── skool-downloader/                 # cloned from origin-public
│   │   ├── .auth/{cookies.txt,           # transferred out-of-band (secrets, chmod 600)
│   │   │         storage_state.json}
│   │   ├── .venv-transcribe-p312/        # py3.12 venv, NON-MLX deps only
│   │   └── downloads  ──► /data/skool/downloads   (symlink)
│   └── course-to-obsidian/               # cloned from origin
│       └── .env                          # transferred out-of-band
├── /data/skool/downloads/<slug>/         # 30 GB videos + nested vault git repos (435 GB partition)
├── /data/skool/whisper-models/           # HF model cache for faster-whisper
└── ~/faster-whisper/compose.yaml         # local OpenAI-compatible whisper (Docker)
```

Transcription flips from `parakeet-mlx` (stage 2) to `transcribe_videos_remote.py` → `http://localhost:8000`.

---

## Server facts (verified 2026-07-06)

- **Host:** `mini-orchestrator64`, Ubuntu 24.04.4, 16-core AMD Ryzen 7 255, 60 GB RAM. User `infra`, SSH `infra@192.168.8.104`.
- **Disk:** `/` = 458 GB (264 GB free); **`/data` (nvme0n1p3) = 458 GB, 435 GB free, owned by `infra`, empty** → video target.
- **No GPU compute path:** Radeon 780M iGPU only; no CUDA. Whisper runs on CPU.
- **Tooling already present:** Node 20/22/24 (nvm; default `v20.20.2`), Python 3.12.3, `ffmpeg`, `yt-dlp`, `uv`, `git`, `bun`, `gbrain`, Claude CLI (`~/.claude`), Docker.
- **Hermes house style (pattern to mirror later):** each instance is a **user-level systemd service** (`~/.config/systemd/user/hermes-gateway*.service`) running one `hermes_cli` Python CLI under a shared venv, differentiated by `--profile` + a per-instance `HERMES_HOME`/`WorkingDirectory` dotfolder. **User lingering is ON** (`loginctl` `Linger=yes` — services survive logout), `Restart=always`, `RestartSec=5`, logs → journald. Scheduled jobs use `.timer` units. Existing Docker services live as `~/<svc>/compose.yaml` projects (hermes, ldr, postiz, supabase, searxng…).
- **Precedent:** a `vault-sync-server.service` already git-commits + pushes an Obsidian vault — directly analogous to pipeline stage 5.

## Local pipeline facts

- `course-to-obsidian/scripts/full_pipeline.sh` runs 6 idempotent stages: scrape (`npm run skool`) → transcribe → HEVC re-encode → Haiku vault build → git commit in vault → gbrain sync.
- **Stage 2 uses `parakeet-mlx` — Apple-Silicon only**, so it cannot run on the AMD server. An alternative already exists: `transcribe_videos_remote.py` uploads to a faster-whisper OpenAI-compatible HTTP server (`/v1/audio/transcriptions`), writing `<title>.remote.md` beside each video. It defaults to `http://192.168.8.165:8000` (that box did **not** respond on 2026-07-06 — treat as unavailable; we run whisper locally instead).
- **Downloads path is hardcoded** to `process.cwd()/downloads` (`skool-downloader/src/cli.ts:523`) → relocate via symlink, no code change.
- **Secrets footprint (gitignored, transfer out-of-band):** `skool-downloader/.auth/{cookies.txt, storage_state.json}` (~6 KB, live Skool tokens) + `course-to-obsidian/.env`.
- Vault dirs (`downloads/<slug>/`) are **nested git repos** (Obsidian vault versioned, no remote) → transfer with `rsync -aH` to preserve `.git` + hardlinks.
- Current data: `downloads/` = 29 GB (`makerschool` 28 G, `startupempire` 1.6 G).

---

## Phases (each independently verifiable)

### Phase 0 — Pre-flight (no changes)
- [x] **`claude` CLI authed as `infra` — CONFIRMED 2026-07-06** (v2.1.201, `claude -p` → `AUTH_OK`). Stage 4 `--backend sdk` works, no API key needed.
- [x] **Node ≥20 — CONFIRMED** (nvm default v20.20.2).
- [x] **Docker present — CONFIRMED** (v29.5.3).
- [ ] Confirm a headed browser is reachable for Skool auth refresh (see Risk 1). The server is a Lubuntu desktop with Firefox/GNOME snaps, so headed Playwright may work directly. **← only remaining pre-flight gate.**

### Phase 1 — Code + env on server (no data yet)
```bash
ssh infra@192.168.8.104
mkdir -p ~/skool /data/skool/downloads /data/skool/whisper-models

cd ~/skool
git clone https://github.com/fabio-dee/online-courses-personal-backup-system.git skool-downloader
git clone https://github.com/fabio-dee/course-to-obsidian.git course-to-obsidian

cd ~/skool/skool-downloader
git remote rename origin origin-public
git remote add origin-private https://<...>/online-courses-personal-backup-system-skool-private.git
git remote add upstream https://github.com/balmasi/skool-downloader.git
git config alias.pushboth '!git push origin-public "$@" && git push origin-private "$@"'   # match CLAUDE.md

# relocate heavy data onto /data
ln -s /data/skool/downloads ~/skool/skool-downloader/downloads

# python venv — NON-MLX deps only (parakeet-mlx would fail on x86)
uv venv --python 3.12 .venv-transcribe-p312
.venv-transcribe-p312/bin/pip install anthropic pyyaml markdownify beautifulsoup4 claude-agent-sdk requests

# node + playwright
npm ci
npx playwright install --with-deps chromium
```
Verify: `readlink downloads` → `/data/skool/downloads`; `node --version`; `.venv-transcribe-p312/bin/python -c "import anthropic, yaml, markdownify, bs4, requests"`.

### Phase 2 — Local faster-whisper (Docker) — DONE 2026-07-06
> **DEPLOYED + VALIDATED end-to-end on real lesson audio.** Image: `ghcr.io/speaches-ai/speaches:latest-cpu` (the maintained successor to `fedirz/faster-whisper-server`; OpenAI-compatible). Model **`deepdml/faster-whisper-large-v3-turbo-ct2`** with **`WHISPER__COMPUTE_TYPE=int8`**. Measured on 180 s of a real `video.hevc.mp4`: **47.9 s → 3.75× realtime**, accurate transcript, **HEVC audio extraction confirmed**. (An 11 s clip only hit ~1× because fixed per-request overhead dominates short audio — the 3.75× figure is the one that matters for the batch.) Container mem ≈ 4 GB, restart=unless-stopped, models cached on `/data`.

Deployed `~/faster-whisper/compose.yaml`:
```yaml
services:
  speaches:
    image: ghcr.io/speaches-ai/speaches:latest-cpu
    container_name: speaches
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      - WHISPER__COMPUTE_TYPE=int8          # NOT default — must be set explicitly for the 3.75× speed
    volumes:
      - /data/skool/whisper-models:/home/ubuntu/.cache/huggingface   # container runs as uid 1000; dir is chmod 777
```
Model is pre-installed (speaches does **not** lazy-load): `curl -X POST http://localhost:8000/v1/models/deepdml/faster-whisper-large-v3-turbo-ct2`. Cache subdir `/data/skool/whisper-models/hub` must exist. Verify: `curl -s http://localhost:8000/v1/models`.

### Phase 3 — Transcription — NOT NEEDED for existing content (corrected 2026-07-06)

**⚠️ Correction:** an earlier note here claimed the vaults were "essentially untranscribed" based on a raw `transcript*.md` file count (~0). **That was wrong.** The vault builder **bakes the transcript into each lesson note** (a `## Transcript` section) and the raw `transcript*.md` files are consumed/renamed and gitignored — so counting them undercounts to zero. Verified by content: **makerschool 615/615 videos and startupempire all have `## Transcript` sections already.** The Mac vaults are **fully transcribed and built**; no backfill is required. A redundant re-transcription batch was started and killed after 2 videos; its stray `transcript.remote.md`/state files were removed, leaving the vaults byte-identical to the Mac.

Also confirmed: **0 `video.mp4`, 644 `video.hevc.mp4`** — originals already re-encoded to HEVC and deleted, so Stage 3 (re-encode) is likewise already done.

**The local whisper server (Phase 2) is therefore for FUTURE scrapes only** — new courses scraped on the server can't use parakeet-mlx (Apple-only), so they'll transcribe via the local whisper path below. It is not needed for the migrated content.
```bash
# FUTURE scrapes only — transcribe a newly-scraped course on the server:
cd ~/skool/course-to-obsidian
../skool-downloader/.venv-transcribe-p312/bin/python scripts/transcribe_videos_remote.py \
  /data/skool/downloads/<new-slug> \
  --servers http://localhost:8000 \
  --model deepdml/faster-whisper-large-v3-turbo-ct2 \
  --filename video.hevc.mp4 --output transcript.remote.md --use-ffmpeg
```
`--output transcript.remote.md` is required (`build_obsidian_vault.py:278` looks for that fixed name). At ~3.7× realtime it's a multi-hour job per large course; run backgrounded.
- [x] **`build_obsidian_vault.py` consumes `.remote.md`/baked transcripts — confirmed** (reads `transcript.md` → renamed `NN. Title.md` → `transcript.parakeet.md`/`transcript.remote.md`).
- [ ] *(Optional, future scrapes only)* add a `SKOOL_TRANSCRIBE_BACKEND=remote` switch to `full_pipeline.sh` stage 2.

### Phase 4 — Data transfer (~30 GB)
From the Mac:
```bash
cd /Users/helldrik/gitRepos/0_Skool_Course_Download/skool-downloader
rsync -aHP --info=progress2 downloads/ infra@192.168.8.104:/data/skool/downloads/
# secrets out-of-band
rsync -a .auth/ infra@192.168.8.104:~/skool/skool-downloader/.auth/
rsync -a ../course-to-obsidian/.env infra@192.168.8.104:~/skool/course-to-obsidian/.env
ssh infra@192.168.8.104 'chmod 600 ~/skool/skool-downloader/.auth/* ~/skool/course-to-obsidian/.env'
```
Verify (compare Mac vs server): `du -sh`, `find … -name '*.md' | wc -l`, `vault_integrity_check.py`,
`transcript_stats.py --dedupe-by-lesson-id`. `-aH` preserves the nested vault `.git` repos.

### Phase 5 — End-to-end canary + cutover
- [ ] Canary on `startupempire` (small): re-run stages 2–5 with download skipped, exercising whisper + vault build + git commit on the server.
- [ ] Full validation run on `makerschool`.
- [ ] Validate a fresh scrape end-to-end (exercises Skool auth on the server — Risk 1).
- [ ] **Only after a clean run:** stop using the Mac for this workload; keep the Mac copy as a cold backup until confident, then delete.
- [ ] Write `RUNBOOK.md` (on-demand invocation + how to bolt on a hermes-style `.service` + `.timer` later).

---

## Risks / open items

1. **Headless Skool auth refresh (biggest wrinkle).** `storage_state.json` expires; `npm run login` needs a real browser. The server *is* a Lubuntu desktop (Firefox/GNOME snaps, display present) → headed Playwright login may work directly on it; **validate**. Fallback: log in on any browser machine and copy `storage_state.json` over (current habit).
2. **Whisper throughput on CPU — measured ~1.7×/worker (2026-07-06).** No CUDA. A ~600-lesson backfill is many hours single-worker; run several workers across the 16 cores to compress it. One-time, idempotent, resumable. Transcript text differs from parakeet but feeds the same classifier — low risk.
3. **Claude CLI auth for stage 4.** Confirm in Phase 0; else `--backend api` + key.
4. **"Sole home" ⇒ the nested vault `.git` becomes the only copy** (no remote by design). Keep the Mac as a cold backup during the transition window before deleting.

## Explicitly out of scope (of the original vault migration)
- Scheduling/automation (on-demand first; seams left to add a hermes-style `.service` + `.timer` later).

---

## Phase 6 — makerschool-coach hermes subsystem (added 2026-07-06; IN SCOPE by later request)

Goal: a hermes coach on **Telegram** that uses the makerschool vault as knowledge (via a **gbrain** brain), running on the server. Discovery: this is a **complete working subsystem on the Mac** (documented in `~/brain/makerschool-coach/REINDEX.md`), so it's a *migration*, not a build.

### Components & migration status
| Component | Mac path | Server action | Status |
|---|---|---|---|
| Vault (source of truth) | `~/gitRepos/…/downloads/makerschool` | already at `/data/skool/downloads/makerschool` | ✅ done |
| Brain (PGLite 297 MB + embeddings) | `~/.gbrain-makerschool` (built w/ gbrain 0.33) | rsync → same path; **`gbrain doctor` = 85/100 OK on server gbrain 0.42** | ✅ done, compatible |
| Coach repo (serve wrapper, wikilinks, playbooks/coaching content) | `~/brain/makerschool-coach` | rsync → same path (no git remote) | ✅ done |
| Hermes profile (config.yaml declares gbrain MCP; SOUL.md persona; .env = Telegram + model + OpenAI keys) | `~/.hermes/profiles/makerschool` | copy → same path on server | ⬜ pending |
| Serve wrapper | `~/brain/makerschool-coach/bin/gbrain-makerschool-serve` (`GBRAIN_HOME=~/.gbrain-makerschool`, `GBRAIN_SOURCE=makerschool-vault`, sources `.env` for `OPENAI_API_KEY`, `gbrain serve`) | works as-is (paths are `$HOME`-relative) | ⬜ verify |
| Launcher + workspace | `bin/hermes-makerschool`, `~/hermes-workspaces/makerschool-coach/makerschool-course` (symlink→vault) | recreate; **repoint symlink → `/data/skool/downloads/makerschool`**; fix hardcoded Mac paths (`~/gitRepos/…`, `~/gitRepos/gbrain`) | ⬜ pending |
| Systemd service | (Mac ran via `~/.local/bin/makerschool` alias) | `hermes gateway install --profile makerschool` (user service, lingering on) | ⬜ pending |

### Telegram channel — where it's configured
The coach's channel lives in the **`makerschool` hermes profile**: `~/.hermes/profiles/makerschool/.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL[_NAME]`) + that profile's `config.yaml` (`telegram.allowed_chats`; also declares the gbrain MCP). Already fully configured on the Mac → **migrating the profile brings the channel with it.** On the server: keep or swap the bot token (swap if the Mac coach must keep running during transition — same token can't run in two places), then `hermes pairing approve` yourself.

### Known follow-up
`gbrain doctor` shows **3 stale sync failures** = the exact Upwork lesson fixed above (`02…💰.md`, `post.md`, `_Day 3.md`) — the Mac brain synced them without `OPENAI_API_KEY`. A server-side `gbrain sync` (key present in brain `.env`) resolves them **and** ingests the `## Post` fix. Remember: PGLite is single-writer — stop the coach gateway before any `gbrain sync`.

### Decision pending
- **Telegram bot token:** reuse the Mac coach's token (Mac coach stops) vs. a fresh @BotFather bot for the server.
