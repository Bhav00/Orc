"""SQLite-backed per-request metrics store.

Complements MetricsStore (in-memory aggregates + JSON snapshot) by persisting
individual request rows so historical queries survive restarts and can be
sliced by time window.

Usage:
    db = MetricsDB("logs/metrics.db")
    await db.init()
    await db.insert_request(...)
    result = await db.query_history(hours=24)
    await db.close()
"""

import os
import time

import aiosqlite

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    session_id        TEXT,
    model             TEXT    NOT NULL,
    stream            INTEGER NOT NULL,
    latency_ms        REAL,
    status            INTEGER,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    error             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_requests_ts    ON requests (ts);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests (model);
"""


class MetricsDB:
    """Append-only SQLite store for per-request metrics.

    Only request rows are stored here.  Process-level events (spawns/kills)
    live in the MetricsStore JSON snapshot instead, as they are aggregate
    counters rather than point-in-time rows.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open (or create) the database and ensure the schema exists."""
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(_CREATE_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def insert_request(
        self,
        *,
        session_id: str,
        model: str,
        stream: bool,
        latency_ms: float,
        status: int,
        prompt_tokens: int,
        completion_tokens: int,
        error: bool,
    ) -> None:
        if self._conn is None:
            return
        await self._conn.execute(
            "INSERT INTO requests "
            "(ts, session_id, model, stream, latency_ms, status, prompt_tokens, completion_tokens, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                session_id,
                model,
                int(stream),
                round(latency_ms, 2),
                status,
                prompt_tokens,
                completion_tokens,
                int(error),
            ),
        )
        await self._conn.commit()

    async def query_history(self, hours: int = 24, model: str | None = None) -> dict:
        """Return per-model aggregates for requests in the last *hours* hours.

        Args:
            hours: Look-back window in hours (default 24).
            model: If given, restrict results to this model ID.

        Returns:
            {"window_hours": N, "models": {model_id: {requests, prompt_tokens,
             completion_tokens, errors, avg_latency_ms}}}
        """
        if self._conn is None:
            return {"window_hours": hours, "models": {}}

        since = int(time.time()) - hours * 3600
        params: tuple
        if model:
            sql = (
                "SELECT model, COUNT(*), "
                "COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
                "COALESCE(SUM(error),0), AVG(latency_ms) "
                "FROM requests WHERE ts >= ? AND model = ? GROUP BY model"
            )
            params = (since, model)
        else:
            sql = (
                "SELECT model, COUNT(*), "
                "COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
                "COALESCE(SUM(error),0), AVG(latency_ms) "
                "FROM requests WHERE ts >= ? GROUP BY model"
            )
            params = (since,)

        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        return {
            "window_hours": hours,
            "models": {
                row[0]: {
                    "requests": row[1],
                    "prompt_tokens": row[2],
                    "completion_tokens": row[3],
                    "errors": row[4],
                    "avg_latency_ms": round(row[5] or 0.0, 1),
                }
                for row in rows
            },
        }
