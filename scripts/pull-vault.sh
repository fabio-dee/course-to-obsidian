#!/usr/bin/env bash
# Mirror a Skool vault from the archive server to this machine, without videos.
# Read-only mirror: the server copy is canonical. Local video files (restored
# with skool-video.sh) and Obsidian workspace state are never touched.
#
# Usage: pull-vault.sh [slug]
# Env: SKOOL_SERVER [infra@192.168.8.104]
#      SKOOL_REMOTE_DOWNLOADS [/media/infra/MYbookext4/library/skool/downloads]
#      SKOOL_LOCAL_DOWNLOADS [<repo-parent>/skool-downloader/downloads]
set -euo pipefail

SLUG=${1:-makerschool}
SERVER=${SKOOL_SERVER:-infra@192.168.8.104}
REMOTE=${SKOOL_REMOTE_DOWNLOADS:-/media/infra/MYbookext4/library/skool/downloads}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
LOCAL=${SKOOL_LOCAL_DOWNLOADS:-$HERE/skool-downloader/downloads}

mkdir -p "$LOCAL/$SLUG"
/usr/bin/rsync -a --delete \
	--exclude='*.mp4' --exclude='*.webm' --exclude='*.mkv' --exclude='*.part' \
	--exclude='*.bak' --exclude='.obsidian/workspace*' --exclude='.DS_Store' \
	"$SERVER:$REMOTE/$SLUG/" "$LOCAL/$SLUG/"

STATUS="$LOCAL/$SLUG/.vault-notes/last-update.md"
echo "$(date '+%F %T') pulled $SLUG from $SERVER"
[[ -f "$STATUS" ]] && grep -m1 '^status:' "$STATUS" || true
