#!/usr/bin/env bash
# Unattended Skool vault update for a headless Linux host (mini-orchestrator).
#
#   1. guard: archive disk mounted, vault on it, single instance
#   2. auth: saved Skool login present and not expired
#   3. download: group classroom URL, --update --allow-layout-fork
#   4. transcribe: lessons without a transcript (onnx Parakeet backend)
#   5. vault build: Haiku tagging via Claude SDK, with a hang watchdog
#   6. encode: libx265 HEVC, then verify + delete sources + repoint viewers
#   7. commit the vault, write .vault-notes/last-update.md (PASSED/PARTIAL/FAILED)
#
# Usage: skool-update.sh [--dry-run]
# Env knobs (defaults in brackets):
#   SKOOL_HOME [~/skool]  SKOOL_SLUG [makerschool]  SKOOL_MOUNT [/media/infra/MYbookext4]
#   SKOOL_SKIP_DOWNLOAD|TRANSCRIBE|VAULT|ENCODE=1  skip a stage
#   SKOOL_ENCODE_CONCURRENCY [4]  SKOOL_SDK_HANG_SEC [300]  SKOOL_AUTH_WARN_DAYS [30]
set -uo pipefail

SKOOL_HOME=${SKOOL_HOME:-$HOME/skool}
SLUG=${SKOOL_SLUG:-makerschool}
MOUNT=${SKOOL_MOUNT:-/media/infra/MYbookext4}
GROUP_URL="https://www.skool.com/$SLUG/classroom"
DL="$SKOOL_HOME/skool-downloader"
C2O="$SKOOL_HOME/course-to-obsidian"
VAULT="$DL/downloads/$SLUG"
PY="$DL/.venv-transcribe-p312/bin/python"
HANG_SEC=${SKOOL_SDK_HANG_SEC:-300}
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1
export PATH="$HOME/.local/bin:$PATH"   # ffmpeg (libx265), claude, codex, yt-dlp

STAMP=$(date +%Y-%m-%d_%H%M)
LOGDIR="$MOUNT/library/skool/logs"
LOG="$LOGDIR/update-$SLUG-$STAMP.log"
STATUS_FILE="$VAULT/.vault-notes/last-update.md"
STAGE="init"
WARNINGS=()

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

write_status() {  # $1 = PASSED|PARTIAL|FAILED, $2 = detail
	mkdir -p "$(dirname "$STATUS_FILE")"
	{
		echo "---"
		echo "status: $1"
		echo "finished_at: $(date -Iseconds)"
		echo "stage: $STAGE"
		echo "host: $(hostname)"
		echo "log: $LOG"
		echo "---"
		echo
		echo "# Skool update: $1"
		echo
		echo "$2"
		if ((${#WARNINGS[@]})); then
			echo
			echo "## Warnings"
			printf -- '- %s\n' "${WARNINGS[@]}"
		fi
	} > "$STATUS_FILE"
}

fail() {
	log "FAILED at stage '$STAGE': $*"
	if [[ -d "$VAULT/.git" && $DRY_RUN == 0 ]]; then
		write_status FAILED "Stage \`$STAGE\` failed: $*"
		git -C "$VAULT" add .vault-notes/last-update.md 2>/dev/null &&
			git -C "$VAULT" commit -q -m "vault: update FAILED at $STAGE ($STAMP)" 2>/dev/null
	fi
	exit 1
}

run_stage() {  # $1 = stage name, rest = command
	STAGE=$1; shift
	log "== stage: $STAGE"
	if ((DRY_RUN)); then log "dry run: $*"; return 0; fi
	"$@" 2>&1 | tee -a "$LOG"
	return "${PIPESTATUS[0]}"
}

# ---- 1. guard -------------------------------------------------------------
mountpoint -q "$MOUNT" || { echo "archive disk not mounted: $MOUNT" >&2; exit 1; }
mkdir -p "$LOGDIR"
REAL_VAULT=$(readlink -f "$VAULT")
[[ "$REAL_VAULT" == "$MOUNT"/* ]] || { echo "vault $REAL_VAULT is not on $MOUNT" >&2; exit 1; }
[[ -d "$VAULT/.git" ]] || { echo "vault git repo missing: $VAULT" >&2; exit 1; }
exec 9>"$LOGDIR/.skool-update-$SLUG.lock"
flock -n 9 || { echo "another skool-update run holds the lock" >&2; exit 1; }
log "start: slug=$SLUG vault=$REAL_VAULT dry_run=$DRY_RUN"

# ---- 2. auth --------------------------------------------------------------
STAGE="auth"
AUTH=$(cd "$DL" && node -e '
const s = require("./.auth/storage_state.json");
const t = (s.cookies || []).find(c => c.name === "auth_token" && String(c.domain).includes("skool.com"));
if (!t || !(t.expires > 0)) { console.log("missing"); process.exit(0); }
console.log(Math.floor((t.expires * 1000 - Date.now()) / 86400000));
' 2>/dev/null) || AUTH="missing"
[[ "$AUTH" =~ ^-?[0-9]+$ ]] || fail "Skool login missing. On the Mac run 'npm run login' in skool-downloader, then copy .auth/ to the server."
((AUTH > 0)) || fail "Skool login expired. On the Mac run 'npm run login' in skool-downloader, then copy .auth/ to the server."
((AUTH < ${SKOOL_AUTH_WARN_DAYS:-30})) && WARNINGS+=("Skool login expires in $AUTH days. Refresh it with 'npm run login' on the Mac.")
log "auth: saved login valid for $AUTH more days"

BEFORE=$(git -C "$VAULT" rev-parse HEAD)

# ---- 3. download ----------------------------------------------------------
if [[ -z "${SKOOL_SKIP_DOWNLOAD:-}" ]]; then
	(cd "$DL" && run_stage download timeout 8h npx tsx src/cli.ts "$GROUP_URL" -o "downloads/$SLUG" --update --allow-layout-fork </dev/null)
	rc=$?
	STAGE="download"
	((rc == 0)) || fail "downloader exited with code $rc"
	grep -q "Failed to fetch courses" "$LOG" && fail "could not fetch the course list (session revoked or Skool changed)"
	n=$(grep -cE "lessons had errors|Failed to download course" "$LOG")
	((n > 0)) && WARNINGS+=("Downloader reported $n course(s) with errors. The next run retries them.")
fi

# ---- 4. transcribe --------------------------------------------------------
if [[ -z "${SKOOL_SKIP_TRANSCRIBE:-}" ]]; then
	run_stage transcribe "$PY" -u "$C2O/scripts/transcribe_videos.py" "$VAULT" \
		--backend onnx --filename video.mp4,video.hevc.mp4 ||
		WARNINGS+=("Some transcriptions failed. Look for transcript.md.FAILED files.")
fi

# ---- 5. vault build (with SDK hang watchdog) ------------------------------
watchdog() {  # kill bundled claude subprocesses that run longer than HANG_SEC
	while sleep 30; do
		ps -eo pid=,etimes=,args= | awk -v lim="$HANG_SEC" \
			'$3 ~ /claude_agent_sdk\/_bundled\/claude/ && $2 > lim {print $1}' |
			while read -r pid; do
				echo "$(date '+%F %T') watchdog: killing hung SDK subprocess $pid" >> "$LOG"
				kill -TERM "$pid" 2>/dev/null
			done
	done
}
if [[ -z "${SKOOL_SKIP_VAULT:-}" ]]; then
	if ((DRY_RUN)); then
		run_stage vault-build "$PY" "$C2O/scripts/build_obsidian_vault.py" "$VAULT" --backend sdk
	else
		watchdog & WD=$!
		run_stage vault-build "$PY" -u "$C2O/scripts/build_obsidian_vault.py" "$VAULT" --backend sdk
		rc=$?
		kill "$WD" 2>/dev/null
		STAGE="vault-build"
		((rc == 0)) || fail "vault builder exited with code $rc"
	fi
fi

# ---- 6. encode + prune ----------------------------------------------------
if [[ -z "${SKOOL_SKIP_ENCODE:-}" ]]; then
	run_stage encode "$PY" -u "$C2O/scripts/reencode_videos.py" "$VAULT" \
		--concurrency "${SKOOL_ENCODE_CONCURRENCY:-4}" --preset medium ||
		WARNINGS+=("Some encodes failed. Their video.mp4 files stay in place.")
	run_stage prune "$PY" "$C2O/scripts/prune_encoded_sources.py" "$VAULT" ||
		WARNINGS+=("Some HEVC copies failed verification. Their sources were kept.")
fi

# ---- 7. commit + status ---------------------------------------------------
STAGE="commit"
if ((DRY_RUN)); then log "dry run complete"; exit 0; fi
NEW_NOTES=$(git -C "$VAULT" status --porcelain -uall -- '*.md' | grep -c '^??')
CHANGED_NOTES=$(git -C "$VAULT" status --porcelain -uall -- '*.md' | grep -c '^ M')
RESULT=PASSED
((${#WARNINGS[@]})) && RESULT=PARTIAL
write_status "$RESULT" "Run $STAMP: $NEW_NOTES new notes, $CHANGED_NOTES changed notes. Previous vault commit: \`${BEFORE:0:8}\`."
git -C "$VAULT" add -A
git -C "$VAULT" commit -q -m "vault: monthly update $STAMP ($NEW_NOTES new, $CHANGED_NOTES changed notes, $RESULT)" ||
	fail "git commit failed"
log "done: $RESULT, $NEW_NOTES new notes, $CHANGED_NOTES changed notes, commit $(git -C "$VAULT" rev-parse --short HEAD)"
