"""Small, local, atomic birthday store for the touchscreen calendar.

The other persistent touchscreen choices live under ``~/.config/aipi5`` and
use a temporary file plus ``replace`` (for example the game settings).  The
calendar follows that same convention.  Lunar dates remain lunar dates in the
file; converting one occurrence to a Gregorian date is presentation work and
must never change what the person entered.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_STORE = Path.home() / ".config" / "aipi5" / "birthdays.json"
MAX_BIRTHDAYS = 500


class BirthdayError(ValueError):
    """A birthday request is malformed or names an entry that does not exist."""


def _number(value, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BirthdayError(f"{label} must be between {low} and {high}") from exc
    if not low <= number <= high:
        raise BirthdayError(f"{label} must be between {low} and {high}")
    return number


def _optional_year(value) -> int | None:
    if value in (None, ""):
        return None
    return _number(value, "birth year", 1800, 2100)


def _clean(payload: dict, entry_id: str) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise BirthdayError("name is required")
    if len(name) > 80:
        raise BirthdayError("name is too long")

    calendar = str(payload.get("calendar", "")).lower()
    if calendar not in ("solar", "lunar"):
        raise BirthdayError("calendar must be solar or lunar")

    month = _number(payload.get("month"), "month", 1, 12)
    day = _number(payload.get("day"), "day", 1, 31 if calendar == "solar" else 30)
    if calendar == "solar":
        # A year-independent validation. February 29 is intentionally valid:
        # it recurs only in leap years, which the browser handles per year.
        maximum = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
        if day > maximum:
            raise BirthdayError("day is not valid for that month")

    note = str(payload.get("note", "")).strip()
    if len(note) > 400:
        raise BirthdayError("note is too long")

    return {
        "id": entry_id,
        "name": name,
        "calendar": calendar,
        "month": month,
        "day": day,
        "leap": bool(payload.get("leap")) if calendar == "lunar" else False,
        "year": _optional_year(payload.get("year")),
        "note": note,
    }


class BirthdayStore:
    """Thread-safe birthday CRUD backed by one human-readable JSON file."""

    def __init__(self, path: Path | str = DEFAULT_STORE):
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("could not read birthdays at %s: %s", self.path, exc)
            return []
        rows = payload.get("birthdays", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            log.warning("birthdays at %s are not a list", self.path)
            return []
        clean: list[dict] = []
        for row in rows[:MAX_BIRTHDAYS]:
            try:
                if not isinstance(row, dict):
                    raise BirthdayError("birthday is not an object")
                entry_id = str(row.get("id", "")).strip() or uuid.uuid4().hex
                clean.append(_clean(row, entry_id))
            except BirthdayError as exc:
                log.warning("ignoring invalid birthday at %s: %s", self.path, exc)
        return clean

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items]

    def save(self, payload: dict) -> dict:
        with self._lock:
            requested = str(payload.get("id", "")).strip()
            if requested:
                index = next((i for i, item in enumerate(self._items)
                              if item["id"] == requested), None)
                if index is None:
                    raise BirthdayError("birthday does not exist")
                entry_id = requested
            else:
                if len(self._items) >= MAX_BIRTHDAYS:
                    raise BirthdayError("birthday list is full")
                index = None
                entry_id = uuid.uuid4().hex

            item = _clean(payload, entry_id)
            updated = list(self._items)
            if index is None:
                updated.append(item)
            else:
                updated[index] = item
            self._write(updated)
            self._items = updated
            return dict(item)

    def delete(self, entry_id: str) -> None:
        with self._lock:
            index = next((i for i, item in enumerate(self._items)
                          if item["id"] == entry_id), None)
            if index is None:
                raise BirthdayError("birthday does not exist")
            updated = list(self._items)
            del updated[index]
            self._write(updated)
            self._items = updated

    def _write(self, items: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"version": 1, "birthdays": items},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            raise BirthdayError(f"could not save birthdays: {exc}") from exc
