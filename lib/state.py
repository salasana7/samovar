"""Project state management backed by SQLite."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DB_FILENAME = ".samovar/state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    plan_json TEXT,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_language TEXT,
    text TEXT NOT NULL,
    url TEXT,
    thread_url TEXT,
    source_ts TEXT,
    collected_at TEXT NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    label TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence_en TEXT,
    unknown_terms_json TEXT,
    run_id INTEGER REFERENCES runs(id),
    classified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    original_label TEXT,
    revised_label TEXT,
    confidence TEXT,
    thread_context_summary TEXT,
    new_lexicon_json TEXT,
    recommendation TEXT,
    run_id INTEGER REFERENCES runs(id),
    investigated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    status TEXT NOT NULL,
    original_label TEXT,
    original_severity TEXT,
    revised_label TEXT,
    revised_severity TEXT,
    reason TEXT,
    run_id INTEGER REFERENCES runs(id),
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT,
    decided_at TEXT NOT NULL
);
"""


class State:
    """Manages the project's SQLite state database."""

    def __init__(self, project_dir: Path):
        self.db_path = project_dir / DB_FILENAME
        self.db_path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- Runs ---

    def start_run(self, plan: Optional[dict] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, plan_json, status) VALUES (?, ?, ?)",
            (_now(), json.dumps(plan) if plan else None, "running"),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str = "completed"):
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
            (_now(), status, run_id),
        )
        self.conn.commit()

    # --- Posts ---

    def add_posts(self, posts: list[dict]):
        for p in posts:
            self.conn.execute(
                """INSERT OR IGNORE INTO posts
                   (post_id, source, source_language, text, url, thread_url,
                    source_ts, collected_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(p["post_id"]),
                    p["source"],
                    p.get("source_language"),
                    p["text"],
                    p.get("url"),
                    p.get("thread_url"),
                    p.get("source_ts"),
                    _now(),
                    json.dumps(p.get("metadata")) if p.get("metadata") else None,
                ),
            )
        self.conn.commit()

    def get_unclassified_posts(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """SELECT p.* FROM posts p
               LEFT JOIN classifications c ON p.post_id = c.post_id
               WHERE c.id IS NULL
               ORDER BY p.collected_at
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_post(self, post_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        return dict(row) if row else None

    # --- Classifications ---

    def add_classifications(self, classifications: list[dict], run_id: int):
        for c in classifications:
            self.conn.execute(
                """INSERT INTO classifications
                   (post_id, label, severity, confidence, evidence_en,
                    unknown_terms_json, run_id, classified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(c["post_id"]),
                    c["label"],
                    c["severity"],
                    c["confidence"],
                    c.get("evidence_en"),
                    json.dumps(c.get("unknown_terms")) if c.get("unknown_terms") else None,
                    run_id,
                    _now(),
                ),
            )
        self.conn.commit()

    def get_flagged_classifications(self) -> list[dict]:
        """Get classifications with low confidence or unknown terms."""
        rows = self.conn.execute(
            """SELECT c.*, p.text, p.url, p.thread_url FROM classifications c
               JOIN posts p ON c.post_id = p.post_id
               WHERE c.confidence = 'low'
                  OR c.unknown_terms_json IS NOT NULL
               ORDER BY c.classified_at"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unreviewed_findings(self) -> list[dict]:
        """Get medium/high severity classifications not yet reviewed."""
        rows = self.conn.execute(
            """SELECT c.*, p.text, p.url, p.thread_url FROM classifications c
               JOIN posts p ON c.post_id = p.post_id
               LEFT JOIN reviews r ON c.post_id = r.post_id
               WHERE r.id IS NULL
                 AND c.severity IN ('medium', 'high')
               ORDER BY c.severity DESC, c.classified_at"""
        ).fetchall()
        return [dict(r) for r in rows]

    def update_classification(
        self, post_id: str, label: str, confidence: str, severity: str | None = None
    ):
        if severity:
            self.conn.execute(
                """UPDATE classifications
                   SET label = ?, confidence = ?, severity = ?
                   WHERE post_id = ? AND id = (
                       SELECT MAX(id) FROM classifications WHERE post_id = ?
                   )""",
                (label, confidence, severity, post_id, post_id),
            )
        else:
            self.conn.execute(
                """UPDATE classifications
                   SET label = ?, confidence = ?
                   WHERE post_id = ? AND id = (
                       SELECT MAX(id) FROM classifications WHERE post_id = ?
                   )""",
                (label, confidence, post_id, post_id),
            )
        self.conn.commit()

    # --- Investigations ---

    def add_investigation(self, investigation: dict, run_id: int):
        self.conn.execute(
            """INSERT INTO investigations
               (post_id, original_label, revised_label, confidence,
                thread_context_summary, new_lexicon_json, recommendation,
                run_id, investigated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(investigation["post_id"]),
                investigation.get("original_label"),
                investigation.get("revised_label"),
                investigation.get("confidence"),
                investigation.get("thread_context_summary"),
                json.dumps(investigation.get("new_lexicon_entries"))
                if investigation.get("new_lexicon_entries")
                else None,
                investigation.get("recommendation"),
                run_id,
                _now(),
            ),
        )
        self.conn.commit()

    # --- Reviews ---

    def add_reviews(self, reviews: list[dict], run_id: int):
        for r in reviews:
            self.conn.execute(
                """INSERT INTO reviews
                   (post_id, status, original_label, original_severity,
                    revised_label, revised_severity, reason,
                    run_id, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(r["post_id"]),
                    r["status"],
                    r.get("original_label"),
                    r.get("original_severity"),
                    r.get("revised_label"),
                    r.get("revised_severity"),
                    r.get("reason"),
                    run_id,
                    _now(),
                ),
            )
        self.conn.commit()

    def get_reviewed_findings(self) -> list[dict]:
        """Get all reviewed findings for reporting."""
        rows = self.conn.execute(
            """SELECT r.*, c.label as classification_label, c.severity,
                      c.evidence_en, p.text, p.url
               FROM reviews r
               JOIN classifications c ON r.post_id = c.post_id
               JOIN posts p ON r.post_id = p.post_id
               WHERE r.status IN ('confirmed', 'reclassified')
               ORDER BY c.severity DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Checkpoints ---

    def add_checkpoint(self, post_id: str, action: str, details: dict):
        self.conn.execute(
            "INSERT INTO checkpoints (post_id, action, details_json, decided_at) VALUES (?, ?, ?, ?)",
            (post_id, action, json.dumps(details), _now()),
        )
        self.conn.commit()

    # --- Summary ---

    def summary(self) -> dict:
        """Return a summary of the project state for the coordinator."""

        def count(query):
            return self.conn.execute(query).fetchone()[0]

        total_posts = count("SELECT COUNT(*) FROM posts")
        classified = count("SELECT COUNT(DISTINCT post_id) FROM classifications")
        unclassified = total_posts - classified
        flagged = count(
            "SELECT COUNT(*) FROM classifications WHERE confidence = 'low'"
        )
        investigated = count("SELECT COUNT(DISTINCT post_id) FROM investigations")
        reviewed = count("SELECT COUNT(DISTINCT post_id) FROM reviews")
        unreviewed_high = count(
            """SELECT COUNT(*) FROM classifications c
               LEFT JOIN reviews r ON c.post_id = r.post_id
               WHERE r.id IS NULL AND c.severity IN ('medium', 'high')"""
        )

        last_run = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

        return {
            "total_posts": total_posts,
            "classified": classified,
            "unclassified": unclassified,
            "flagged_low_confidence": flagged,
            "investigated": investigated,
            "reviewed": reviewed,
            "unreviewed_medium_high": unreviewed_high,
            "last_run": dict(last_run) if last_run else None,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
