#!/usr/bin/env bash
# Convenience launcher for reencode_videos.py against both Skool vaults.
#
# Usage:
#   scripts/reencode_all.sh              # concurrency=2, preset=medium (balanced, Mac usable)
#   scripts/reencode_all.sh 4            # concurrency=4 (AFK mode)
#   scripts/reencode_all.sh 6 slow       # slower preset for a few % smaller files
#
# Idempotent — safe to Ctrl-C and re-run. Interrupted encodes restart cleanly.
# Originals are NEVER deleted by this script. Delete them manually after verifying
# a batch plays back fine.

set -Eeuo pipefail

CONCURRENCY=${1:-2}
PRESET=${2:-medium}

REPO_ROOT="/Users/helldrik/gitRepos/0_Skool_Course_Download"
PY="$REPO_ROOT/skool-downloader/.venv-transcribe-p312/bin/python"
SCRIPT="$REPO_ROOT/course-to-obsidian/scripts/reencode_videos.py"
LOG="/tmp/reencode.log"

cd "$REPO_ROOT"

printf '\033[1;34m[reencode]\033[0m concurrency=%s preset=%s\n' "$CONCURRENCY" "$PRESET"
printf '\033[1;34m[reencode]\033[0m log: %s\n' "$LOG"
printf '\033[1;34m[reencode]\033[0m Ctrl-C is safe — resume by re-running this script\n'
if [[ "$CONCURRENCY" -le 2 ]]; then
  printf '\033[1;33m[hint]\033[0m if AFK, bump to concurrency=4 for ~3h speedup:\n'
  printf '\033[1;33m[hint]\033[0m   course-to-obsidian/scripts/reencode_all.sh 4\n'
fi
printf '\n'

"$PY" "$SCRIPT" \
  skool-downloader/downloads/startupempire \
  skool-downloader/downloads/makerschool \
  --concurrency "$CONCURRENCY" \
  --preset "$PRESET" \
  --verbose 2>&1 | tee "$LOG"
