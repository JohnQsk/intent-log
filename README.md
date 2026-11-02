# intent-log

A small local service: **pick a task → work 20 minutes → write one line of feedback → rest 5 minutes → repeat**, with every segment appended to a log you can read back as a table.

It merges [task-quick-web](https://github.com/JohnQsk/task-quick-web) (a dstask task page) and [rest.reminder](https://github.com/JohnQsk/rest.reminder) (server-side cycle timer plus a feedback page) into one Python process, one port, one page, standard library only.

## Why this table

The interesting question is not how long you sat there. It is: **what did you actually do, did it move, where did you get stuck, what is next.** So each row is a task name, start and end time, real duration, and one self-written line — not an efficiency score.

## Run it

```bat
cd path\to\intent-log
python app.py
```

Opens `http://127.0.0.1:8770`. Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--port` | 8770 | local port (taskweb uses 8765; both can run) |
| `--work-seconds` | 1200 | length of one work segment |
| `--break-seconds` | 300 | length of one break |
| `--data-dir` | `~/.intent-log` | where state and records live |
| `--no-browser` | off | do not open a browser automatically |

Short test cycle: `python app.py --work-seconds 20 --break-seconds 5 --data-dir .tmp-data --no-browser`

## Data

- `~/.intent-log/state.json` — current cycle state; refreshing the page or restarting the service keeps a pending feedback.
- `~/.intent-log/sessions.jsonl` — one line per finished segment, append-only. Old records are never rewritten.

A line looks like this:

```json
{"schema_version": 1, "session_id": "...", "task_id": "<dstask uuid>",
 "task_title": "implement LRU cache", "task_project": "algorithms",
 "started_at": "2026-11-02T09:05:00+08:00", "ended_at": "2026-11-02T09:25:00+08:00",
 "planned_seconds": 1200, "work_seconds": 1200,
 "feedback_submitted_at": "2026-11-02T09:26:10+08:00",
 "status": "progress", "content": "Main path works, edge cases still failing",
 "next_action": "same_task"}
```

`status`: `progress` / `task_done` / `blocked` / `interrupted` / `off_task`
`next_action`: `same_task` / `choose_task` / `finish`

Tasks still live in dstask. Only "Task done" in the feedback marks the dstask task complete; reaching 20 minutes **never** completes a task or touches dstask's own time tracking.

## Task sync

`dstask sync` is `git pull` followed by `git push` inside `~/.dstask`, so the sync target is simply that repository's `origin` remote. The **Task sync** panel at the bottom of the page shows:

- the current `origin` URL and branch
- how many commits you are ahead of or behind `origin/<branch>`
- whether the work tree has uncommitted changes
- the result of the last sync

You can paste a different git URL and press **Save remote** to repoint `origin` (it only runs `git remote set-url`, task data is untouched), then press **Sync now** to run `dstask sync`.

The URL must be a plain git URL (`https://`, `ssh://`, `git://` or `git@host:path`); anything else is refused before git is called.

## Log sync

The session log can live in its own private repository so a session written on
one machine shows up on the other. It is deliberately **not** the task
repository: a log conflict must never block `dstask sync`, and dstask's own
`git add -A` commits must not swallow log rows.

Setup once per machine, with the data directory itself as the repository:

```bat
cd %USERPROFILE%\.intent-log
git init -b main
git remote add origin git@host:you/intent-log-data.git
```

Two files matter in that repository:

```
.gitignore      state.json, *.tmp        (device-local state never leaves the machine)
.gitattributes  sessions.jsonl text eol=lf merge=union
```

`state.json` holds the running cycle and must stay local — syncing it would let
two machines fight over one timer. `merge=union` lets both devices' appended
rows survive a merge instead of turning into conflict markers.

Then use the **Log sync** panel: it shows the remote, branch, ahead/behind
counts, and last result; **Sync now** commits new rows, fetches, merges and
pushes. Two safety rules are built in:

- the pull is a **merge, not a rebase**, because a rebase does not run the
  union driver and can silently drop the other device's rows;
- before pushing, the set of `session_id`s is compared with what it was before
  the pull, and if any row would disappear nothing is pushed.

A real merge conflict (for example, no union attribute) is reported with the
path to fix, and the repository is left clean rather than half-merged.

If you only ever use one machine, skip all of this: the panel simply reports
that the data directory is not a git repository.

## HTTP API (for agents)

Bound to `127.0.0.1` only. Every POST needs the `X-Intent-Log: 1` header; bodies are JSON.

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/api/state` | current phase, task, remaining seconds, today's totals, version |
| GET | `/api/tasks` | open dstask tasks |
| GET | `/api/tasks/completed` | recently completed tasks |
| GET | `/api/records?date=YYYY-MM-DD` | records for one day, plus the list of days |
| GET | `/api/sync` | dstask repository config and last sync result |
| GET | `/api/log-sync` | log repository config and last sync result |
| POST | `/api/cycle/start` | `{"task_id": "<uuid>"}`, or no task at all |
| POST | `/api/cycle/pause` | pause, or resume if already paused |
| POST | `/api/cycle/end` | end the segment early and go to feedback |
| POST | `/api/cycle/cancel` | discard the segment, writes nothing |
| POST | `/api/cycle/feedback` | `{"status","content","next_action"}` |
| POST | `/api/cycle/skip-break` | end the break now |
| POST | `/api/tasks` | `{"description","project"}` creates a task |
| POST | `/api/sync` | runs `dstask sync` |
| POST | `/api/sync/remote` | `{"url"}` repoints the task repository's `origin` |
| POST | `/api/log-sync` | commit, merge and push the session log |
| POST | `/api/log-sync/remote` | `{"url"}` repoints the log repository's `origin` |

```bash
curl -s http://127.0.0.1:8770/api/state
curl -s -X POST http://127.0.0.1:8770/api/cycle/start -H "X-Intent-Log: 1" -H "Content-Type: application/json" -d "{}"
```

## Boundaries in this version

- No day planning and no scoring; it never guesses whether a segment was "good".
- No window or browser monitoring.
- No CSV export.
- Log sync is manual: nothing is committed or pushed until you press **Sync now**.
- Time while the service is stopped is not counted as work: on restart the cycle resumes from the last saved state instead of adding offline time.
- The 20-20-20 eye reminder from `rest.reminder` is not ported yet.

## Tests

```bat
python -m unittest discover -s tests
python tests\smoke_http.py
```

- `tests/test_cycle.py` — cycle state machine (pause excluded from work time, early end, restart recovery, failed write keeps feedback, one row per segment, duplicate `session_id` rows hidden).
- `tests/test_sync.py` — git remote reading, repointing, URL validation, log commit/push, union merge keeping both devices' rows, a denied push when the merge would lose a row, and a reported conflict that leaves the repository clean. All on temporary repositories.
- `tests/smoke_http.py` — starts the real service and walks the whole path. It deliberately starts a cycle **without** a task so nothing in the real task list is created or completed, and it never calls `POST /api/sync` or `POST /api/log-sync` (both would touch real repositories or the network).

## License

MIT. Ideas and parts of the implementation come from the author's own `task-quick-web` and `rest.reminder`, both MIT.
