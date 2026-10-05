#!/usr/bin/env bash
# Copy one lesson's video back from the archive server into the local vault,
# so the offline viewer (index.html) and Obsidian can play it.
#
# Usage: skool-video.sh "<lesson folder>"      (path inside the local vault)
#        skool-video.sh --remove "<lesson folder>"   (delete the local copy again)
# Env: same as pull-vault.sh (SKOOL_SERVER, SKOOL_REMOTE_DOWNLOADS, SKOOL_LOCAL_DOWNLOADS)
set -euo pipefail

SERVER=${SKOOL_SERVER:-infra@192.168.8.104}
REMOTE=${SKOOL_REMOTE_DOWNLOADS:-/media/infra/MYbookext4/library/skool/downloads}
HERE=$(cd "$(dirname "$0")/../.." && pwd)
LOCAL=${SKOOL_LOCAL_DOWNLOADS:-$HERE/skool-downloader/downloads}

REMOVE=0
[[ "${1:-}" == "--remove" ]] && { REMOVE=1; shift; }
[[ $# -eq 1 ]] || { echo "usage: $0 [--remove] \"<lesson folder>\"" >&2; exit 2; }

DIR=$(cd "$1" && pwd)
LOCAL_REAL=$(cd "$LOCAL" && pwd)
[[ "$DIR" == "$LOCAL_REAL"/* ]] || { echo "not inside $LOCAL_REAL: $DIR" >&2; exit 2; }
REL=${DIR#"$LOCAL_REAL"/}

if ((REMOVE)); then
	rm -f "$DIR/video.hevc.mp4" "$DIR/video.mp4"
	echo "removed local video in $REL"
	exit 0
fi

for name in video.hevc.mp4 video.mp4; do
	if ssh "$SERVER" test -f "$(printf '%q' "$REMOTE/$REL/$name")"; then
		# scp uses SFTP, so paths with spaces need no remote shell quoting
		scp -q "$SERVER:$REMOTE/$REL/$name" "$DIR/$name"
		echo "restored $REL/$name ($(du -h "$DIR/$name" | cut -f1))"
		exit 0
	fi
done
echo "no video on the server for $REL" >&2
exit 1
