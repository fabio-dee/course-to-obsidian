#!/usr/bin/env bash
# End-to-end: download a Skool course → transcribe videos → re-encode videos → build Obsidian vault → git init.
#
# Usage:
#   scripts/full_pipeline.sh <skool-url> [slug]
#   scripts/full_pipeline.sh https://www.skool.com/makerschool/classroom/foo-bar
#
# Steps (each is idempotent — safe to re-run):
#   1. npm run skool <url>                     → downloads/<slug>/
#   2. transcribe_videos.py downloads/<slug>   → writes transcript.md beside each video
#   3. reencode_videos.py downloads/<slug>     → writes optimized video.hevc.mp4 variants
#   4. build_obsidian_vault.py ...             → rename/enrich/MOCs/Haiku tags
#   5. git init + gitignore + initial commit   (inside the vault, videos excluded)
#   6. gbrain sync + wikilinks-import          (Hermes brain re-index; makerschool only)
#
# Env knobs:
#   SKOOL_SKIP_DOWNLOAD=1    skip stage 1 (useful when scrape already done)
#   SKOOL_SKIP_TRANSCRIBE=1  skip stage 2 (useful when re-running vault build only)
#   SKOOL_SKIP_REENCODE=1    skip stage 3
#   SKOOL_REENCODE_PRESET    x265 preset for stage 3 (default: medium)
#   SKOOL_REENCODE_DELETE_SOURCE=1 delete source video.mp4 after successful HEVC encode
#   SKOOL_SKIP_VAULT=1       skip stage 4
#   SKOOL_SKIP_GIT=1         skip stage 5
#   SKOOL_SKIP_GBRAIN=1      skip stage 6
#   SKOOL_BACKEND=sdk|api    Haiku backend for stage 3 (default: sdk)
#   SKOOL_VENV=path          python venv; absolute or relative to $REPO
#                            (default: $SKOOL_DOWNLOADER_DIR/.venv-transcribe-p312)
#   SKOOL_DOWNLOADER_DIR     sibling path to the skool-downloader repo
#                            (default: <parent-of-this-repo>/skool-downloader)
#   SKOOL_GBRAIN_SLUG        vault slug eligible for stage 5 (default: makerschool)
#   SKOOL_GBRAIN_HOME        gbrain data dir (default: ~/.gbrain-makerschool)
#   SKOOL_GBRAIN_BIN         gbrain executable (default: ~/.bun/bin/gbrain)
#   SKOOL_GBRAIN_WIKILINKS   wikilinks-import.ts path
#                            (default: ~/brain/makerschool-coach/scripts/wikilinks-import.ts)
#   SKOOL_BUN_BIN            bun executable (default: ~/.bun/bin/bun)
#
# Layout assumption: this repo (course-to-obsidian) and skool-downloader live as
# siblings in a shared parent folder. Override with SKOOL_DOWNLOADER_DIR if not.

set -Eeuo pipefail

err() { printf '\033[1;31m[pipeline]\033[0m %s\n' "$*" >&2; }
log() { printf '\033[1;34m[pipeline]\033[0m %s\n' "$*"; }
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
	[[ $u =~ skool\.com/([^/]+)(/|$) ]] || {
		err "cannot parse slug from URL: $u"
		exit 2
	}
	printf '%s' "${BASH_REMATCH[1]}"
}

SLUG=${SLUG_OVERRIDE:-$(derive_slug "$URL")}
VAULT="$DOWNLOADER_DIR/downloads/$SLUG"
log "slug:       $SLUG"
log "downloader: $DOWNLOADER_DIR"
log "vault:      $VAULT"

# -------- 1. download --------
if [[ -z "${SKOOL_SKIP_DOWNLOAD:-}" ]]; then
	step "1/5  download via npm run skool (in $DOWNLOADER_DIR)"
	(cd "$DOWNLOADER_DIR" && npm run skool "$URL")
else
	log "skip stage 1 (SKOOL_SKIP_DOWNLOAD set)"
fi

[[ -d "$VAULT" ]] || {
	err "expected vault dir missing: $VAULT"
	exit 1
}

# -------- 2. transcribe --------
if [[ -z "${SKOOL_SKIP_TRANSCRIBE:-}" ]]; then
	step "2/5  transcribe videos (parakeet-mlx, idempotent)"
	# New downloads arrive as video.mp4. Historical/archive-optimized files may
	# only have video.hevc.mp4. Check both so backfills don't require a one-off
	# manual command.
	"$PY" "$REPO/scripts/transcribe_videos.py" "$VAULT" --filename video.mp4
	"$PY" "$REPO/scripts/transcribe_videos.py" "$VAULT" --filename video.hevc.mp4
else
	log "skip stage 2 (SKOOL_SKIP_TRANSCRIBE set)"
fi

# -------- 3. re-encode videos --------
if [[ -z "${SKOOL_SKIP_REENCODE:-}" ]]; then
	step "3/5  re-encode source video.mp4 files to archive HEVC (idempotent)"
	REENCODE_PRESET=${SKOOL_REENCODE_PRESET:-medium}
	REENCODE_ARGS=("$VAULT" --preset "$REENCODE_PRESET")
	if [[ -n "${SKOOL_REENCODE_DELETE_SOURCE:-}" ]]; then
		REENCODE_ARGS+=(--delete-source)
	fi
	"$PY" "$REPO/scripts/reencode_videos.py" "${REENCODE_ARGS[@]}"
else
	log "skip stage 3 (SKOOL_SKIP_REENCODE set)"
fi

# -------- 4. build vault --------
# Capture pre-build baselines for the post-build guardrail (defends against
# duplication bugs like the 26 GB vault doubling that silently shipped in
# commit fa419b9). Only counts files that aren't inside .git/.
vault_size_kb() { du -sk "$1" 2>/dev/null | awk '{print $1}'; }
vault_md_count() { find "$1" -name "*.md" -not -path "*/.git/*" 2>/dev/null | wc -l | tr -d ' '; }

PRE_SIZE=$(vault_size_kb "$VAULT")
PRE_COUNT=$(vault_md_count "$VAULT")
log "pre-build baseline: ${PRE_SIZE} KB, ${PRE_COUNT} markdown files"

if [[ -z "${SKOOL_SKIP_VAULT:-}" ]]; then
	step "4/5  build Obsidian vault (rename, frontmatter, MOCs, Haiku tags)"
	BACKEND=${SKOOL_BACKEND:-sdk}
	"$PY" "$REPO/scripts/build_obsidian_vault.py" "$VAULT" --backend "$BACKEND"
else
	log "skip stage 3 (SKOOL_SKIP_VAULT set)"
fi

# -------- 3.5 sanity guardrail --------
# Refuse to auto-commit if the vault grew >50% in either size or md-count
# during stage 3. Skipped when the pre-build vault was empty (fresh scrape).
POST_SIZE=$(vault_size_kb "$VAULT")
POST_COUNT=$(vault_md_count "$VAULT")
log "post-build state:    ${POST_SIZE} KB, ${POST_COUNT} markdown files"

GROWTH_VIOLATION=0
if [[ "${PRE_SIZE:-0}" -gt 0 ]]; then
	if awk -v a="$PRE_SIZE" -v b="$POST_SIZE" 'BEGIN { exit !((b - a) / a > 0.5) }'; then
		GROWTH_VIOLATION=1
	fi
fi
if [[ "${PRE_COUNT:-0}" -gt 0 ]]; then
	if awk -v a="$PRE_COUNT" -v b="$POST_COUNT" 'BEGIN { exit !((b - a) / a > 0.5) }'; then
		GROWTH_VIOLATION=1
	fi
fi

if [[ "$GROWTH_VIOLATION" -eq 1 ]]; then
	err "================================================================"
	err "❌ Vault doubled in size — likely duplication bug. NOT committing."
	err "----------------------------------------------------------------"
	err "  size  : ${PRE_SIZE} KB → ${POST_SIZE} KB  (>50% growth)"
	err "  files : ${PRE_COUNT} → ${POST_COUNT} markdown files  (>50% growth)"
	err "----------------------------------------------------------------"
	err "Inspect ${VAULT} manually."
	err "If the growth is intentional, run:"
	err "  cd \"${VAULT}\" && git add -A && git commit"
	err "================================================================"
	exit 1
fi

# -------- 3.5  vault integrity gate --------
INTEGRITY_SCRIPT="$(dirname "$0")/vault_integrity_check.py"
if [[ -f "$INTEGRITY_SCRIPT" ]]; then
	log "running vault_integrity_check..."
	set +e
	"$PY" "$INTEGRITY_SCRIPT" "$VAULT"
	INTEGRITY_RC=$?
	set -e
	if [[ "$INTEGRITY_RC" -eq 2 ]]; then
		err "================================================================"
		err "❌ Integrity check FAILED. Refusing to commit."
		err "Inspect ${VAULT} manually and re-run with SKOOL_SKIP_GIT to bypass."
		err "================================================================"
		exit 1
	elif [[ "$INTEGRITY_RC" -eq 1 ]]; then
		log "integrity check: WARN (non-fatal, continuing)"
	else
		log "integrity check: OK"
	fi
fi

# -------- 5. git init + commit --------
if [[ -z "${SKOOL_SKIP_GIT:-}" ]]; then
	step "5/5  git init + commit"
	cd "$VAULT"
	if [[ ! -d .git ]]; then
		git init -q
		log "git initialized"
	fi
	if [[ ! -f .gitignore ]]; then
		cat >.gitignore <<'EOF'
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

# -------- 6. gbrain sync + wikilinks import --------
# Re-index the vault into the Hermes makerschool-coach brain (PGLite at
# $GBRAIN_HOME). Auto-enabled only for the makerschool slug, since the brain
# is course-specific. PGLite is single-writer, so we refuse to proceed if the
# Hermes gateway is up — stop it manually, re-run, then restart.
GBRAIN_SLUG=${SKOOL_GBRAIN_SLUG:-makerschool}
GBRAIN_HOME=${SKOOL_GBRAIN_HOME:-$HOME/.gbrain-makerschool}
GBRAIN_BIN=${SKOOL_GBRAIN_BIN:-$HOME/.bun/bin/gbrain}
GBRAIN_WIKILINKS=${SKOOL_GBRAIN_WIKILINKS:-$HOME/brain/makerschool-coach/scripts/wikilinks-import.ts}
BUN_BIN=${SKOOL_BUN_BIN:-$HOME/.bun/bin/bun}

if [[ -n "${SKOOL_SKIP_GBRAIN:-}" ]]; then
	log "skip stage 5 (SKOOL_SKIP_GBRAIN set)"
elif [[ "$SLUG" != "$GBRAIN_SLUG" ]]; then
	log "skip stage 5 (slug '$SLUG' ≠ gbrain slug '$GBRAIN_SLUG')"
elif [[ ! -x "$GBRAIN_BIN" ]]; then
	log "skip stage 5 (gbrain not found at $GBRAIN_BIN)"
elif [[ ! -d "$GBRAIN_HOME" ]]; then
	log "skip stage 5 (GBRAIN_HOME missing: $GBRAIN_HOME)"
else
	step "5/5  gbrain sync + wikilinks import"

	# Preflight: PGLite is single-writer — refuse if the brain is being held open
	# by the gateway or a stray gbrain serve. Don't auto-kill; the user may have
	# it running for a reason.
	if PGREP_OUT=$(pgrep -fl 'hermes gateway run|bun.*gbrain serve' 2>/dev/null); then
		if [[ -n "$PGREP_OUT" ]]; then
			err "================================================================"
			err "⚠️  Hermes gateway / gbrain serve is running — PGLite is single-writer."
			err "----------------------------------------------------------------"
			err "$PGREP_OUT"
			err "----------------------------------------------------------------"
			err "Stop it (Ctrl+C in its terminal) and re-run with:"
			err "  SKOOL_SKIP_DOWNLOAD=1 SKOOL_SKIP_TRANSCRIBE=1 SKOOL_SKIP_VAULT=1 SKOOL_SKIP_GIT=1 \\"
			err "    $0 \"\" $SLUG"
			err "Or rerun the full pipeline with SKOOL_SKIP_GBRAIN=1 to skip indexing."
			err "================================================================"
			exit 1
		fi
	fi

	# Source $GBRAIN_HOME/.env if present — embeds API keys (OPENAI_API_KEY for
	# embeddings, etc.) the same way the MCP serve wrapper does. Without this,
	# sync runs but every page fails embedding and the brain is left in a
	# "Sync BLOCKED" state.
	if [[ -f "$GBRAIN_HOME/.env" ]]; then
		log "sourcing $GBRAIN_HOME/.env"
		# shellcheck disable=SC1090
		set -a
		source "$GBRAIN_HOME/.env"
		set +a
	fi

	# GBRAIN_SOURCE must match what the MCP serve wrapper uses, otherwise sync
	# tries to update existing pages under a different source and fails with
	# 'createVersion failed: page "..." (source=default) not found'.
	# Default mirrors ~/brain/makerschool-coach/bin/gbrain-makerschool-serve.
	export GBRAIN_SOURCE="${GBRAIN_SOURCE:-${SKOOL_GBRAIN_SOURCE:-makerschool-vault}}"

	# 5a. Incremental git→brain sync. The brain pulls only what changed since the
	# last sync via the vault's nested git history (committed in stage 4).
	log "gbrain sync --repo $VAULT  (source=$GBRAIN_SOURCE)"
	GBRAIN_HOME="$GBRAIN_HOME" GBRAIN_SOURCE="$GBRAIN_SOURCE" "$GBRAIN_BIN" sync --repo "$VAULT"

	# 5b. Wikilink edge import. Idempotent via --resume; safe to re-run.
	if [[ -f "$GBRAIN_WIKILINKS" ]]; then
		if [[ ! -x "$BUN_BIN" ]]; then
			err "bun not found at $BUN_BIN — skipping wikilinks import"
		else
			WIKILINKS_DIR=$(dirname "$GBRAIN_WIKILINKS")
			log "bun run wikilinks-import.ts --resume --vault $VAULT"
			(cd "$WIKILINKS_DIR" && GBRAIN_HOME="$GBRAIN_HOME" "$BUN_BIN" run "$GBRAIN_WIKILINKS" --resume --vault "$VAULT")
		fi
	else
		log "skip wikilinks import (script missing: $GBRAIN_WIKILINKS)"
	fi
fi

step "DONE"
log "open in Obsidian: $VAULT"
log "root MOC: $VAULT/$(ls "$VAULT" | grep -Ei '\.md$' | head -1 || echo '<course root>.md')"
