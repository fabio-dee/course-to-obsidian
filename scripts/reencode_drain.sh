#!/usr/bin/env bash
# Graceful drain control for reencode_videos.py.
#
# Usage:
#   reencode_drain.sh start   # create sentinel -> workers stop picking new tasks
#   reencode_drain.sh stop    # remove sentinel -> workers resume picking tasks
#   reencode_drain.sh status  # show sentinel state + running ffmpegs
#
# After `start`, the Python script's in-flight ffmpegs (usually N = concurrency)
# finish normally — their sidecars get written, no progress is lost. Any task
# pulled AFTER the sentinel exists returns status="drained" immediately.
#
# Once all in-flight jobs finish, the Python process exits cleanly. Then you
# can relaunch at a different concurrency with:
#   reencode_drain.sh stop && course-to-obsidian/scripts/reencode_all.sh 2
set -u
SENTINEL=/tmp/reencode_drain
cmd=${1:-status}
case "$cmd" in
  start)
    touch "$SENTINEL"
    echo "[drain] sentinel created: $SENTINEL"
    echo "[drain] in-flight ffmpegs will finish; no new tasks will start."
    n=$(pgrep -f "ffmpeg.*hevc" | wc -l | tr -d ' ')
    echo "[drain] currently running ffmpegs: $n (wait for them to finish)"
    ;;
  stop)
    rm -f "$SENTINEL"
    echo "[drain] sentinel removed"
    ;;
  status)
    if [[ -f "$SENTINEL" ]]; then
      echo "[drain] ACTIVE (sentinel exists: $SENTINEL)"
    else
      echo "[drain] inactive"
    fi
    n=$(pgrep -f "ffmpeg.*hevc" | wc -l | tr -d ' ')
    echo "[drain] running ffmpegs: $n"
    p=$(pgrep -f "reencode_videos.py" | head -1)
    if [[ -n "$p" ]]; then
      echo "[drain] reencode_videos.py alive (pid $p)"
    else
      echo "[drain] reencode_videos.py not running"
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
