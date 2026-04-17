#!/usr/bin/env bash
# End-to-end: download a Skool course → transcribe videos → build Obsidian vault → git init.
#
# Usage:
#   scripts/full_pipeline.sh <skool-url> [slug]
#   scripts/full_pipeline.sh https://www.skool.com/makerschool/classroom/foo-bar
#
# Steps (each is idempotent — safe to re-run):
#   1. npm run skool <url>                     → downloads/<slug>/
#   2. transcribe_videos.py downloads/<slug>   → writes transcript.md beside each video
#   3. build_obsidian_vault.py ...             → rename/enrich/MOCs/Haiku tags
#   4. git init + gitignore + initial commit   (inside the vault, videos excluded)
#
# Env knobs:
#   SKOOL_SKIP_DOWNLOAD=1    skip stage 1 (useful when scrape already done)
#   SKOOL_SKIP_TRANSCRIBE=1  skip stage 2 (useful when re-running vault build only)
#   SKOOL_SKIP_VAULT=1       skip stage 3
#   SKOOL_SKIP_GIT=1         skip stage 4
#   SKOOL_BACKEND=sdk|api    Haiku backend for stage 3 (default: sdk)
#   SKOOL_VENV=path          python venv; absolute or relative to $REPO
#                            (default: $SKOOL_DOWNLOADER_DIR/.venv-transcribe-p312)
#   SKOOL_DOWNLOADER_DIR     sibling path to the skool-downloader repo
#                            (default: <parent-of-this-repo>/skool-downloader)
#
# Layout assumption: this repo (course-to-obsidian) and skool-downloader live as
# siblings in a shared parent folder. Override with SKOOL_DOWNLOADER_DIR if not.

set -Eeuo pipefail

err()  { printf '\033[1;31m[pipeline]\033[0m %s\n' "$*" >&2; }
log()  { printf '\033[1;34m[pipeline]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1;32m▶ %s\033[0m\n' "$*"; }

URL=${1:-}
SLUG_OVERRIDE=${2:-}
if [[ -z "$URL" && -z "${SKOOL_SKIP_DOWNLOAD:-}${SLUG_OVERRIDE}" ]]; then
  err "usage: $0 <skool-url> [slug]"
  exit 2
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SCRIPT_DIR/.." && pwd)

# Locate the sibling skool-downloader repo (used for stage 1 + as default venv host).
# Override via SKOOL_DOWNLOADER_DIR. Default: <wrapper>/skool-downloader where
# <wrapper> is the parent of $REPO.
DOWNLOADER_DIR=${SKOOL_DOWNLOADER_DIR:-$(cd "$REPO/.." && pwd)/skool-downloader}
if [[ ! -d "$DOWNLOADER_DIR" ]]; then
  err "skool-downloader not found at $DOWNLOADER_DIR"
  err "set SKOOL_DOWNLOADER_DIR=/path/to/skool-downloader or place it as a sibling of $REPO"
  exit 2
fi

# Allow absolute or repo-relative SKOOL_VENV; default falls back to the downloader's venv
# (we intentionally share one venv across both repos — it already has parakeet-mlx + anthropic + etc).
if [[ -n "${SKOOL_VENV:-}" && "${SKOOL_VENV:0:1}" == "/" ]]; then
  PY="$SKOOL_VENV/bin/python"
elif [[ -n "${SKOOL_VENV:-}" ]]; then
  PY="$REPO/$SKOOL_VENV/bin/python"
else
  PY="$DOWNLOADER_DIR/.venv-transcribe-p312/bin/python"
fi
if [[ ! -x "$PY" ]]; then
  err "python venv not found at $PY"
  err "either point SKOOL_VENV at an existing venv, or create one:"
  err "  python3.12 -m venv $DOWNLOADER_DIR/.venv-transcribe-p312"
  err "  $DOWNLOADER_DIR/.venv-transcribe-p312/bin/pip install -r $REPO/scripts/transcribe_videos.requirements.txt anthropic pyyaml markdownify beautifulsoup4 claude-agent-sdk"
  exit 2
fi

# Derive slug (community name) from URL: https://www.skool.com/<slug>/classroom/...
derive_slug() {
  local u=$1
  [[ $u =~ skool\.com/([^/]+)(/|$) ]] || { err "cannot parse slug from URL: $u"; exit 2; }
  printf '%s' "${BASH_REMATCH[1]}"
}

SLUG=${SLUG_OVERRIDE:-$(derive_slug "$URL")}
VAULT="$DOWNLOADER_DIR/downloads/$SLUG"
log "slug:       $SLUG"
log "downloader: $DOWNLOADER_DIR"
log "vault:      $VAULT"

# -------- 1. download --------
if [[ -z "${SKOOL_SKIP_DOWNLOAD:-}" ]]; then
  step "1/4  download via npm run skool (in $DOWNLOADER_DIR)"
  (cd "$DOWNLOADER_DIR" && npm run skool "$URL")
else
  log "skip stage 1 (SKOOL_SKIP_DOWNLOAD set)"
fi

[[ -d "$VAULT" ]] || { err "expected vault dir missing: $VAULT"; exit 1; }

# -------- 2. transcribe --------
if [[ -z "${SKOOL_SKIP_TRANSCRIBE:-}" ]]; then
  step "2/4  transcribe videos (parakeet-mlx, idempotent)"
  "$PY" "$REPO/scripts/transcribe_videos.py" "$VAULT"
else
  log "skip stage 2 (SKOOL_SKIP_TRANSCRIBE set)"
fi

# -------- 3. build vault --------
if [[ -z "${SKOOL_SKIP_VAULT:-}" ]]; then
  step "3/4  build Obsidian vault (rename, frontmatter, MOCs, Haiku tags)"
  BACKEND=${SKOOL_BACKEND:-sdk}
  "$PY" "$REPO/scripts/build_obsidian_vault.py" "$VAULT" --backend "$BACKEND"
else
  log "skip stage 3 (SKOOL_SKIP_VAULT set)"
fi

# -------- 4. git init + commit --------
if [[ -z "${SKOOL_SKIP_GIT:-}" ]]; then
  step "4/4  git init + commit"
  cd "$VAULT"
  if [[ ! -d .git ]]; then
    git init -q
    log "git initialized"
  fi
  if [[ ! -f .gitignore ]]; then
    cat > .gitignore <<'EOF'
# Skool-vault gitignore — excludes heavy binaries + redundant originals
*.mp4
*.mov
*.webm
*.mkv
*.m4a
*.mp3
*.wav
*.png
*.jpg
*.jpeg
*.gif
*.pdf
*.html
*.json
assets/
resources/
transcript.md
transcript.remote.md
transcript.parakeet.md
.DS_Store
.obsidian/workspace*
.obsidian/cache
EOF
    log "wrote .gitignore"
  fi
  git add -A
  if git diff --cached --quiet; then
    log "nothing to commit"
  else
    git -c user.email=vault@local -c user.name=vault commit -q -m "vault: pipeline build ($(date -u +%Y-%m-%dT%H:%MZ))"
    log "committed snapshot: $(git log -1 --format='%h %s')"
  fi
else
  log "skip stage 4 (SKOOL_SKIP_GIT set)"
fi

step "DONE"
log "open in Obsidian: $VAULT"
log "root MOC: $VAULT/$(ls "$VAULT" | grep -Ei '\.md$' | head -1 || echo '<course root>.md')"
