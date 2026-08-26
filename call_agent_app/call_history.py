"""Durable PSTN call history and playable WAV recordings."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import wave
from datetime import datetime, timezone
from pathlib import Path

APP_ID = "call-agent"


def default_data_dir() -> Path:
    explicit = os.environ.get("AW_CALL_AGENT_DATA")
    if explicit:
        return Path(explicit)
    workspace_home = os.environ.get("AW_WORKSPACE_HOME")
    if workspace_home:
        return Path(workspace_home) / "data" / APP_ID
    workspace_dir = os.environ.get("AW_WORKSPACE_CONTAINER_DIR")
    if workspace_dir:
        return Path(workspace_dir) / ".aw-workspace" / "data" / APP_ID
    # Standalone/CI runs are not necessarily inside /opt/aw-workspace (and a
    # shared runner may deliberately make that path read-only). The Tier-2
    # image always sets AW_CALL_AGENT_DATA=/app/data, so this fallback is only
    # for direct `python -m call_agent_app` usage and test collection.
    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
    return base / "aw-call-agent"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallStore:
    """SQLite metadata plus one mono PCM WAV file per call.

    SQLite is used only by this app and the recordings live beside it under
    ``fs:workspace-data``.  No audio or call data is written into the app's
    package directory, so updates cannot erase the history.
    """

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.recordings = self.root / "recordings"
        self.root.mkdir(parents=True, exist_ok=True)
        self.recordings.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "calls.sqlite3"
        self._lock = threading.RLock()
        self._writers: dict[tuple[str, str], wave.Wave_write] = {}
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY,
                    direction TEXT NOT NULL,
                    remote_number TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    sample_bytes INTEGER NOT NULL DEFAULT 0,
                    recording_file TEXT,
                    transcript TEXT NOT NULL DEFAULT '',
                    agent_text TEXT NOT NULL DEFAULT '',
                    run_ids TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
            if "agent_recording_file" not in columns:
                conn.execute("ALTER TABLE calls ADD COLUMN agent_recording_file TEXT")

    def ensure_call(self, call_id: str, direction: str = "inbound",
                    remote_number: str = "") -> dict:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO calls
                   (id, direction, remote_number, status, started_at)
                   VALUES (?, ?, ?, 'active', ?)""",
                (call_id, direction, remote_number, _now()),
            )
        return self.get(call_id)

    def start_recording(self, call_id: str, direction: str = "in") -> Path:
        with self._lock:
            key = (call_id, direction)
            suffix = "" if direction == "in" else "-agent"
            if key in self._writers:
                return self.recordings / f"{call_id}{suffix}.wav"
            self.ensure_call(call_id)
            path = self.recordings / f"{call_id}{suffix}.wav"
            writer = wave.open(str(path), "wb")
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            self._writers[key] = writer
            with self._connect() as conn:
                column = ("recording_file" if direction == "in"
                          else "agent_recording_file")
                conn.execute(f"UPDATE calls SET {column}=? WHERE id=?",
                             (path.name, call_id))
            return path

    def append_pcm(self, call_id: str, pcm: bytes, direction: str = "in"):
        if not pcm:
            return
        with self._lock:
            key = (call_id, direction)
            if key not in self._writers:
                self.start_recording(call_id, direction)
            self._writers[key].writeframesraw(pcm)
            # Duration follows caller audio only; agent audio has its own WAV.
            if direction == "in":
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE calls SET sample_bytes=sample_bytes+? WHERE id=?",
                        (len(pcm), call_id),
                    )

    def append_text(self, call_id: str, *, transcript: str = "",
                    agent_text: str = "", run_id: str = ""):
        # Ensure the row before opening the transaction used below. Creating
        # it from another connection after SELECT can leave this connection
        # reading an older SQLite snapshot.
        self.ensure_call(call_id)
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT transcript,agent_text,run_ids FROM calls WHERE id=?",
                               (call_id,)).fetchone()
            runs = json.loads(row["run_ids"] or "[]")
            if run_id and run_id not in runs:
                runs.append(run_id)
            conn.execute(
                "UPDATE calls SET transcript=?, agent_text=?, run_ids=? WHERE id=?",
                ("\n".join(x for x in (row["transcript"], transcript) if x),
                 "\n".join(x for x in (row["agent_text"], agent_text) if x),
                 json.dumps(runs), call_id),
            )

    def finish(self, call_id: str, status: str = "completed", error: str = ""):
        with self._lock:
            for direction in ("in", "out"):
                writer = self._writers.pop((call_id, direction), None)
                if writer is not None:
                    writer.close()
            with self._connect() as conn:
                row = conn.execute("SELECT sample_bytes FROM calls WHERE id=?",
                                   (call_id,)).fetchone()
                if row is None:
                    return
                duration = float(row["sample_bytes"] or 0) / (8000 * 2)
                conn.execute(
                    """UPDATE calls SET status=?, ended_at=?, duration_seconds=?, error=?
                       WHERE id=?""",
                    (status, _now(), duration, error, call_id),
                )

    def get(self, call_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def recording_path(self, call_id: str) -> Path | None:
        row = self.get(call_id)
        if not row or not row.get("recording_file"):
            return None
        path = (self.recordings / row["recording_file"]).resolve()
        if path.parent != self.recordings.resolve() or not path.is_file():
            return None
        return path

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["run_ids"] = json.loads(item.get("run_ids") or "[]")
        item["has_recording"] = bool(item.get("recording_file"))
        return item

    def close(self):
        with self._lock:
            for writer in self._writers.values():
                writer.close()
            self._writers.clear()
