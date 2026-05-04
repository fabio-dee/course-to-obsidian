# reencode_drain.sh

Graceful drain for `reencode_videos.py`. Lets in-flight ffmpegs finish; stops new ones starting. No progress lost.

## Commands

```bash
reencode_drain.sh start    # stop picking new tasks
reencode_drain.sh stop     # resume picking tasks
reencode_drain.sh status   # show sentinel + running ffmpegs
```

## How it works

- `start` → creates `/tmp/reencode_drain`
- `process_one()` checks sentinel at task start → returns `drained` if present
- In-flight tasks (already past check) finish normally, sidecars write, Python exits when pool empties
- `stop` → removes sentinel

## Change concurrency without loss

```bash
reencode_drain.sh start                    # from another terminal
reencode_drain.sh status                   # wait until "running ffmpegs: 0"
reencode_drain.sh stop
course-to-obsidian/scripts/reencode_all.sh 2   # or 3, 4, 6…
```

Already-done videos are skipped via sidecar cache on resume.
