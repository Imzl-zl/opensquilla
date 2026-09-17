"""Goal attribution migration preserves earlier bills and incomplete history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yoyo import read_migrations

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "V045__goal_runtime_accounting.py"


def _load_migration():
    migrations = read_migrations(str(MIGRATION.parent))
    migration = next(item for item in migrations if item.id == MIGRATION.stem)
    migration.load()
    return migration.module


def test_v045_marks_existing_goals_partial_and_preserves_usage_rows() -> None:
    migration = _load_migration()
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE usage_events (event_id TEXT PRIMARY KEY, session_id TEXT, "
            "session_epoch INTEGER, total_tokens INTEGER)"
        )
        conn.execute(
            "CREATE TABLE session_goals (goal_id TEXT PRIMARY KEY, total_tokens INTEGER)"
        )
        conn.execute("INSERT INTO usage_events VALUES ('old-call', 'old-session', 2, 75)")
        conn.execute("INSERT INTO session_goals VALUES ('old-goal', 50)")
        migration.apply_step(conn)
        migration.apply_step(conn)
        assert conn.execute("SELECT * FROM usage_events").fetchone() == (
            "old-call", "old-session", 2, 75, None, None,
        )
        assert conn.execute(
            "SELECT total_tokens, token_budget, budget_tokens_used, usage_accounting_version, "
            "background, usage_coverage, usage_accounting_started_at_ms FROM session_goals"
        ).fetchone() == (50, None, 0, 0, 0, "partial_history", None)
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(usage_events)")}
        assert "idx_usage_events_goal" in indexes
        migration.rollback_step(conn)
        assert conn.execute("SELECT goal_id FROM usage_events").fetchone() == (None,)
