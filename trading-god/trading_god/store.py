"""SQLite journal of every proposal and what happened to it.

The journal is also the agent's feedback loop: it reads back which proposals
were approved, rejected (with the person's reason) or blocked, and learns from
them on the next run.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable

PENDING = "pending"  # waiting for a person
REFUSED = "refused"  # failed a risk check when proposed, never queued
PLACING = "placing"  # approved, order being sent
PLACED = "placed"  # order accepted by moomoo
BLOCKED = "blocked"  # approved, but failed a risk check against fresh account state
FAILED = "failed"  # approved, but moomoo rejected the order or could not be reached
REJECTED = "rejected"  # a person said no
WITHDRAWN = "withdrawn"  # the agent took it back
EXPIRED = "expired"  # nobody decided in time

STATUSES = (PENDING, REFUSED, PLACING, PLACED, BLOCKED, FAILED, REJECTED, WITHDRAWN, EXPIRED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    ref_price REAL,
    notional REAL,
    thesis TEXT NOT NULL DEFAULT '',
    playbook TEXT NOT NULL DEFAULT '',
    stop_loss REAL,
    take_profit REAL,
    horizon TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT '',
    risk_report TEXT NOT NULL DEFAULT '{}',
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    broker_order_id TEXT,
    broker_response TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals (status);
"""


@dataclass(frozen=True)
class Proposal:
    id: int
    created_at: str
    expires_at: str
    status: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float | None
    ref_price: float | None
    notional: float | None
    thesis: str
    playbook: str
    stop_loss: float | None
    take_profit: float | None
    horizon: str
    confidence: str
    risk_report: dict[str, Any]
    decided_at: str | None
    decision_note: str
    broker_order_id: str | None
    broker_response: dict[str, Any] | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Proposal":
        data = dict(row)
        data["risk_report"] = json.loads(data["risk_report"] or "{}")
        data["broker_response"] = json.loads(data["broker_response"]) if data["broker_response"] else None
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_INSERT_COLUMNS = frozenset(f.name for f in fields(Proposal)) - {"id"}


class ProposalStore:
    """Small wrapper over one SQLite file; safe to share between processes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def add(self, **values: Any) -> Proposal:
        unknown = set(values) - _INSERT_COLUMNS
        if unknown:
            raise ValueError(f"unknown proposal field(s): {', '.join(sorted(unknown))}")
        values = dict(values)
        values["risk_report"] = json.dumps(values.get("risk_report") or {})
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(f"INSERT INTO proposals ({columns}) VALUES ({marks})", tuple(values.values()))
            proposal_id = cursor.lastrowid
        found = self.get(int(proposal_id))
        assert found is not None
        return found

    def get(self, proposal_id: int) -> Proposal | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return Proposal.from_row(row) if row else None

    def list(self, statuses: Iterable[str] | None = None, limit: int = 50) -> list[Proposal]:
        query = "SELECT * FROM proposals"
        params: list[Any] = []
        wanted = list(statuses or [])
        if wanted:
            query += f" WHERE status IN ({', '.join('?' for _ in wanted)})"
            params.extend(wanted)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Proposal.from_row(row) for row in rows]

    def count(self, status: str) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM proposals WHERE status = ?", (status,)).fetchone()[0])

    def count_placed_buys_since(self, since_iso: str, *, exclude_id: int | None = None) -> int:
        """Count buys sent to moomoo since ``since_iso`` (placing counts: it may have reached the broker).

        ``exclude_id`` leaves out the proposal being approved, which is already ``placing``.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE side = 'buy' AND status IN (?, ?) AND decided_at >= ? AND id != ?",
                (PLACED, PLACING, since_iso, -1 if exclude_id is None else exclude_id),
            ).fetchone()
        return int(row[0])

    def claim(self, proposal_id: int, now_iso: str, note: str) -> bool:
        """Move a live pending proposal to ``placing``; only one caller can win."""
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE proposals SET status = ?, decided_at = ?, decision_note = ? "
                "WHERE id = ? AND status = ? AND expires_at > ?",
                (PLACING, now_iso, note, proposal_id, PENDING, now_iso),
            )
            return cursor.rowcount == 1

    def decide(self, proposal_id: int, status: str, now_iso: str, note: str) -> bool:
        """Close a pending proposal without placing it (rejected or withdrawn)."""
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE proposals SET status = ?, decided_at = ?, decision_note = ? WHERE id = ? AND status = ?",
                (status, now_iso, note, proposal_id, PENDING),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        proposal_id: int,
        status: str,
        *,
        broker_order_id: str | None = None,
        broker_response: dict[str, Any] | None = None,
        error: str | None = None,
        risk_report: dict[str, Any] | None = None,
    ) -> None:
        """Record how a ``placing`` proposal ended."""
        assignments = ["status = ?", "broker_order_id = ?", "broker_response = ?", "error = ?"]
        params: list[Any] = [
            status,
            broker_order_id,
            json.dumps(broker_response) if broker_response is not None else None,
            error,
        ]
        if risk_report is not None:
            assignments.append("risk_report = ?")
            params.append(json.dumps(risk_report))
        params.extend([proposal_id, PLACING])
        with closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE proposals SET {', '.join(assignments)} WHERE id = ? AND status = ?", params)

    def expire(self, now_iso: str) -> int:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE proposals SET status = ?, decided_at = ? WHERE status = ? AND expires_at <= ?",
                (EXPIRED, now_iso, PENDING, now_iso),
            )
            return cursor.rowcount
