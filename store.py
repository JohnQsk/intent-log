"""Append-only JSONL store for completed work sessions.

One line per finished 20-minute work segment. Old records are never rewritten:
the web table is only a view over this file.
"""

import json
from pathlib import Path

SCHEMA_VERSION = 1


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record):
        """Append one record. Raises OSError when the file cannot be written."""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()

    def _iter(self):
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return
        except OSError:
            return

    def records(self, day=None):
        """Records for one local date (YYYY-MM-DD), oldest first."""
        items = list(self._iter())
        if day:
            items = [
                item
                for item in items
                if str(item.get("started_at", ""))[:10] == day
            ]
        items.sort(key=lambda item: str(item.get("started_at", "")))
        return items

    def days(self):
        days = {
            str(item.get("started_at", ""))[:10]
            for item in self._iter()
            if item.get("started_at")
        }
        return sorted((day for day in days if day), reverse=True)

    def summary_for(self, day):
        records = self.records(day)
        return {
            "date": day,
            "sessions": len(records),
            "work_seconds": sum(int(item.get("work_seconds") or 0) for item in records),
        }
