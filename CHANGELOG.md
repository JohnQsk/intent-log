# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-11-02

### Added

- Log sync: the data directory can be its own private git repository, so
  sessions written on one machine appear on the other.
- **Log sync** panel and `GET /api/log-sync`, `POST /api/log-sync`,
  `POST /api/log-sync/remote`.
- Pull uses a merge instead of a rebase: a rebase does not run the `union`
  merge driver and can silently drop the other device's rows.
- Post-merge guard: the set of `session_id`s is compared before and after the
  pull, and nothing is pushed if a row would be lost.
- `SessionStore` hides duplicate rows that share a `session_id`.

### Changed

- `syncer.py` split into `GitRepo`, `DstaskRepo` and `LogRepo`; the two
  repositories are configured and synced independently.
- Documented the one-time `git init` setup and the required `.gitignore` /
  `.gitattributes` contents.

## [0.1.1] - 2026-11-02

### Added

- Task sync panel: shows the `origin` URL, branch, ahead/behind counts, dirty
  state and the result of the last sync.
- Repoint the dstask repository from the page (`git remote set-url origin`)
  and run `dstask sync` with one button.
- `GET /api/sync`, `POST /api/sync`, `POST /api/sync/remote` for agents.
- Git URL validation: only `https://`, `http://`, `ssh://`, `git://` and
  `git@host:path` are accepted; everything else is rejected before git runs.
- `version` is reported by `GET /api/state` and shown in the header.
- `CHANGELOG.md`.

### Changed

- The interface is now in English by default; all server-side error messages
  are in English too.
- `README.md` rewritten in English, including the sync model and the full API
  table.
- Example data in the docs and tests no longer contains personal study
  material.

### Fixed

- Pending feedback is still preserved when the log file cannot be written, and
  the UI keeps the typed text so it can be retried.

## [0.1.0] - 2026-11-02

### Added

- One local service (stdlib only) with the cycle state machine
  `idle → work ⇄ work_paused → feedback → break ⇄ break_paused → idle`.
- Task selection from dstask, adding tasks, and "Task done" → `dstask done`.
- Feedback form with status, one line of text and next action.
- Append-only `~/.intent-log/sessions.jsonl` with `schema_version: 1`.
- Table view per day, with a date picker.
- HTTP API for agents, bound to `127.0.0.1`, guarded by the
  `X-Intent-Log: 1` header.
- Restart and refresh recovery; feedback draft kept in `localStorage`.
