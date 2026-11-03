"""Transactional research state with source identity and immutable decision versions."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_FILENAME = ".samovar/state.db"
SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
 finished_at TEXT, plan_json TEXT, status TEXT NOT NULL DEFAULT 'running');
CREATE TABLE posts (post_id TEXT PRIMARY KEY, source TEXT NOT NULL,
 source_post_id TEXT NOT NULL, source_language TEXT, text TEXT NOT NULL, url TEXT,
 thread_url TEXT, source_ts TEXT, collected_at TEXT NOT NULL, metadata_json TEXT,
 UNIQUE(source, source_post_id));
CREATE TABLE classifications (id INTEGER PRIMARY KEY AUTOINCREMENT,
 post_id TEXT NOT NULL REFERENCES posts(post_id), label TEXT NOT NULL,
 severity TEXT NOT NULL, confidence TEXT NOT NULL, evidence_en TEXT,
 unknown_terms_json TEXT, run_id INTEGER REFERENCES runs(id), classified_at TEXT NOT NULL);
CREATE TABLE investigations (id INTEGER PRIMARY KEY AUTOINCREMENT,
 post_id TEXT NOT NULL REFERENCES posts(post_id), original_label TEXT, revised_label TEXT,
 confidence TEXT, thread_context_summary TEXT, new_lexicon_json TEXT, recommendation TEXT,
 run_id INTEGER REFERENCES runs(id), investigated_at TEXT NOT NULL);
CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,
 post_id TEXT NOT NULL REFERENCES posts(post_id),
 classification_id INTEGER REFERENCES classifications(id), status TEXT NOT NULL,
 original_label TEXT, original_severity TEXT, revised_label TEXT, revised_severity TEXT,
 reason TEXT, run_id INTEGER REFERENCES runs(id), reviewed_at TEXT NOT NULL);
CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT,
 post_id TEXT NOT NULL REFERENCES posts(post_id), action TEXT NOT NULL,
 details_json TEXT, decided_at TEXT NOT NULL);
CREATE TABLE escalations (id INTEGER PRIMARY KEY AUTOINCREMENT,
 post_id TEXT NOT NULL REFERENCES posts(post_id), review_id INTEGER UNIQUE REFERENCES reviews(id),
 classification_id INTEGER REFERENCES classifications(id), run_id INTEGER REFERENCES runs(id),
 reason TEXT, created_at TEXT NOT NULL, resolved_at TEXT,
 checkpoint_id INTEGER REFERENCES checkpoints(id));
CREATE TABLE migration_archive (id INTEGER PRIMARY KEY, table_name TEXT NOT NULL,
 reason TEXT NOT NULL, row_json TEXT NOT NULL);
CREATE INDEX idx_classifications_post ON classifications(post_id, id);
CREATE INDEX idx_reviews_classification ON reviews(classification_id, id);
CREATE VIEW current_classifications AS SELECT c.* FROM classifications c
 WHERE c.id = (SELECT MAX(c2.id) FROM classifications c2 WHERE c2.post_id = c.post_id);
"""


def internal_post_id(source: str, source_post_id: str) -> str:
    """An unambiguous, stable encoding, including when either component contains ':'."""
    return "source:" + json.dumps([str(source), str(source_post_id)], ensure_ascii=True, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, project_dir: Path):
        self.db_path = Path(project_dir) / DB_FILENAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._init_schema()
        except Exception:
            self.conn.close()
            raise

    def _init_schema(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("State database was created by a newer Samovar version")
        if version == SCHEMA_VERSION:
            return
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        names = ["runs", "posts", "classifications", "investigations", "reviews", "checkpoints"]
        # Rebuild inside a single transaction. Orphaned legacy records are archived, not erased.
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                snapshots = {n: [dict(r) for r in self.conn.execute(f'SELECT * FROM {n}')] if n in tables else [] for n in names}
                for name in reversed(names):
                    self.conn.execute(f"DROP TABLE IF EXISTS {name}")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        self.conn.execute(statement)
                mapping = {}
                for p in snapshots["posts"]:
                    old = str(p["post_id"])
                    p["source_post_id"] = str(p.get("source_post_id", old))
                    p["post_id"] = internal_post_id(p["source"], p["source_post_id"])
                    mapping[old] = p["post_id"]
                run_ids = {r["id"] for r in snapshots["runs"]}
                for name in names:
                    for row in snapshots[name]:
                        if name not in ("runs", "posts"):
                            if str(row["post_id"]) not in mapping:
                                self._archive(name, row, "Missing legacy post")
                                continue
                            row["post_id"] = mapping[str(row["post_id"])]
                        if row.get("run_id") is not None and row["run_id"] not in run_ids:
                            self._archive(name, row, "Missing legacy run; imported with null run_id")
                            row["run_id"] = None
                        if name == "reviews":
                            # The old schema cannot prove which version was reviewed.
                            row["classification_id"] = None
                        self._insert(name, row)
                        if name == "reviews" and row["status"] == "escalated":
                            self._insert("escalations", {"post_id": row["post_id"], "review_id": row["id"], "reason": row.get("reason"), "run_id": row.get("run_id"), "created_at": row["reviewed_at"]})
                if self.conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("State migration failed foreign key validation")
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def _archive(self, table, row, reason):
        self._insert("migration_archive", {"table_name": table, "reason": reason, "row_json": json.dumps(row)})

    def _insert(self, table, row):
        keys = list(row)
        return self.conn.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", [row[k] for k in keys]).lastrowid

    def close(self):
        self.conn.close()

    def start_run(self, plan: Optional[dict] = None) -> int:
        with self.conn:
            return self._insert("runs", {"started_at": _now(), "plan_json": json.dumps(plan) if plan else None, "status": "running"})

    def finish_run(self, run_id, status="completed"):
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at=?, status=? WHERE id=?", (_now(), status, run_id))

    def add_posts(self, posts):
        with self.conn:
            for p in posts:
                local_id = str(p.get("source_post_id", p["post_id"]))
                self.conn.execute("""INSERT INTO posts
                 (post_id, source, source_post_id, source_language, text, url, thread_url, source_ts, collected_at, metadata_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, source_post_id) DO NOTHING""",
                 (internal_post_id(p["source"], local_id), p["source"], local_id, p.get("source_language"), p["text"], p.get("url"), p.get("thread_url"), p.get("source_ts"), _now(), json.dumps(p.get("metadata")) if p.get("metadata") else None))

    def _rows(self, query, args=()):
        return [dict(r) for r in self.conn.execute(query, args)]

    def get_unclassified_posts(self, limit=50):
        return self._rows("""SELECT p.* FROM posts p WHERE NOT EXISTS
         (SELECT 1 FROM classifications c WHERE c.post_id=p.post_id) ORDER BY p.collected_at, p.post_id LIMIT ?""", (limit,))

    def get_post(self, post_id):
        rows = self._rows("SELECT * FROM posts WHERE post_id=?", (str(post_id),))
        if rows:
            return rows[0]
        rows = self._rows("SELECT * FROM posts WHERE source_post_id=?", (str(post_id),))
        if len(rows) > 1:
            raise ValueError("Ambiguous source-local post ID; use the source-qualified post_id")
        return rows[0] if rows else None

    def get_thread_posts(self, post_id, limit=100):
        """Return already collected context, never fetch a source URL implicitly."""
        post = self.get_post(post_id)
        if not post:
            raise ValueError(f"Unknown post ID: {post_id}")
        if not post.get("thread_url"):
            return [post]
        return self._rows("""SELECT * FROM posts WHERE source=? AND thread_url=?
         ORDER BY source_ts, collected_at, post_id LIMIT ?""",
         (post["source"], post["thread_url"], limit))

    def _post_id(self, post_id):
        post = self.get_post(post_id)
        if not post:
            raise ValueError(f"Unknown post ID: {post_id}")
        return post["post_id"]

    def get_classification(self, post_id):
        post = self.get_post(post_id)
        if not post:
            return None
        rows = self._rows("SELECT * FROM current_classifications WHERE post_id=?", (post["post_id"],))
        return rows[0] if rows else None

    def _current(self, post_id):
        row = self.conn.execute("SELECT * FROM current_classifications WHERE post_id=?", (post_id,)).fetchone()
        if not row:
            raise ValueError(f"Post has no classification: {post_id}")
        return dict(row)

    def _classification(self, c, run_id):
        return self._insert("classifications", {"post_id": self._post_id(c["post_id"]), "label": c["label"], "severity": c["severity"], "confidence": c["confidence"], "evidence_en": c.get("evidence_en"), "unknown_terms_json": json.dumps(c["unknown_terms"]) if c.get("unknown_terms") else c.get("unknown_terms_json"), "run_id": run_id, "classified_at": _now()})

    def add_classifications(self, classifications, run_id):
        with self.conn:
            for c in classifications:
                self._classification(c, run_id)

    def update_classification(self, post_id, label, confidence, severity=None):
        with self.conn:
            c = self._current(self._post_id(post_id))
            c.update(label=label, confidence=confidence, severity=severity or c["severity"])
            return self._classification(c, c["run_id"])

    def add_escalation(self, post_id, reason, run_id=None):
        """Keep investigator requests pending until an explicit human checkpoint."""
        with self.conn:
            post_id = self._post_id(post_id)
            current = self.get_classification(post_id)
            return self._insert("escalations", {"post_id": post_id, "reason": reason, "run_id": run_id, "classification_id": current["id"] if current else None, "created_at": _now()})

    def get_pending_escalations(self):
        return self._rows("""SELECT p.post_id, c.id, c.label, c.severity, c.confidence, c.evidence_en,
         c.unknown_terms_json, c.run_id, c.classified_at, p.text, p.url, p.thread_url, p.source, p.source_post_id,
         e.id AS escalation_id, e.classification_id AS escalation_classification_id,
         e.run_id AS escalation_run_id, e.reason AS escalation_reason, 'escalated' AS status,
         c.label AS classification_label, e.reason AS reason FROM escalations e
         JOIN posts p ON p.post_id=e.post_id
         LEFT JOIN current_classifications c ON c.post_id=e.post_id WHERE e.resolved_at IS NULL
         ORDER BY e.id""")

    def get_flagged_classifications(self):
        rows = self._rows("""SELECT c.*, p.text, p.url, p.thread_url, p.source, p.source_post_id
         FROM current_classifications c JOIN posts p ON p.post_id=c.post_id
         WHERE c.confidence='low' OR (c.unknown_terms_json IS NOT NULL AND c.unknown_terms_json != '[]')""")
        by_post = {r["post_id"]: r for r in rows}
        for e in self.get_pending_escalations():
            previous = by_post.get(e["post_id"])
            if previous and previous.get("escalation_id") is not None:
                e["escalation_reason"] = (previous["escalation_reason"] or "") + "\n" + (e["escalation_reason"] or "")
                e["reason"] = e["escalation_reason"]
            by_post[e["post_id"]] = e
        return list(by_post.values())

    def get_unreviewed_findings(self):
        return self._rows("""SELECT c.*, p.text, p.url, p.thread_url, p.source, p.source_post_id
         FROM current_classifications c JOIN posts p ON p.post_id=c.post_id
         WHERE c.severity IN ('medium','high') AND NOT EXISTS
         (SELECT 1 FROM reviews r WHERE r.classification_id=c.id)
         ORDER BY c.classified_at, c.id""")

    def _investigation(self, investigation, run_id):
        row = {k: investigation.get(k) for k in ("original_label", "revised_label", "confidence", "thread_context_summary", "recommendation")}
        row.update(post_id=self._post_id(investigation["post_id"]), run_id=run_id, investigated_at=_now(), new_lexicon_json=json.dumps(investigation["new_lexicon_entries"]) if investigation.get("new_lexicon_entries") else None)
        return self._insert("investigations", row)

    def add_investigation(self, investigation, run_id):
        with self.conn:
            return self._investigation(investigation, run_id)

    def apply_investigation(self, investigation, run_id):
        """Commit the investigation and its consequential decision together."""
        with self.conn:
            post_id = self._post_id(investigation["post_id"])
            recommendation = investigation["recommendation"]
            if recommendation not in ("confirm", "reclassify", "dismiss", "needs_human"):
                raise ValueError("Invalid investigation recommendation")
            current = self.get_classification(post_id)
            if investigation.get("classification_id") is not None and (
                not current or current["id"] != investigation["classification_id"]
            ):
                raise ValueError("Investigation refers to a stale classification version")
            investigation_id = self._investigation(investigation, run_id)
            if recommendation == "reclassify":
                if not current:
                    raise ValueError("Cannot reclassify an unclassified post")
                current.update(label=investigation["revised_label"], confidence=investigation["confidence"],
                    evidence_en=investigation["thread_context_summary"] +
                    "\n\nPrior classification evidence (before revision): " + (current.get("evidence_en") or ""))
                self._classification(current, run_id)
            elif recommendation in ("dismiss", "needs_human"):
                self._insert("escalations", {"post_id": post_id,
                    "classification_id": current["id"] if current else None,
                    "run_id": run_id, "reason": investigation.get("thread_context_summary"),
                    "created_at": _now()})
            entries = investigation.get("new_lexicon_entries", [])
            if entries:
                proposals = "; ".join(f"{entry['term']}: {entry['meaning']} (confidence: {entry['confidence']})" for entry in entries)
                latest = self.get_classification(post_id)
                self._insert("escalations", {"post_id": post_id,
                    "classification_id": latest["id"] if latest else None,
                    "run_id": run_id, "reason": "Proposed lexicon entries require analyst validation: " + proposals,
                    "created_at": _now()})
            return investigation_id

    def add_reviews(self, reviews, run_id):
        with self.conn:
            for r in reviews:
                post_id = self._post_id(r["post_id"])
                c = self._current(post_id)
                if r.get("classification_id") is not None and r["classification_id"] != c["id"]:
                    raise ValueError("Review refers to a stale classification version")
                if r["status"] not in ("confirmed", "reclassified", "downgraded", "dismissed", "escalated"):
                    raise ValueError("Invalid review status")
                version = c["id"]
                if r["status"] in ("reclassified", "downgraded"):
                    revised = dict(c)
                    revised.update(label=r.get("revised_label") or c["label"], severity=r.get("revised_severity") or c["severity"], confidence="high")
                    version = self._classification(revised, run_id)
                row = {k: r.get(k) for k in ("status", "revised_label", "revised_severity", "reason")}
                row.update(post_id=post_id, classification_id=version, original_label=c["label"], original_severity=c["severity"], run_id=run_id, reviewed_at=_now())
                review_id = self._insert("reviews", row)
                if r["status"] == "escalated":
                    self._insert("escalations", {"post_id": post_id, "review_id": review_id, "classification_id": version, "run_id": run_id, "reason": r.get("reason"), "created_at": _now()})

    def get_reviewed_findings(self):
        rows = self._rows("""SELECT r.*, c.label AS classification_label, c.severity,
         c.evidence_en, p.text, p.url, p.source, p.source_post_id FROM reviews r
         JOIN current_classifications c ON r.classification_id=c.id AND r.post_id=c.post_id
         JOIN posts p ON p.post_id=r.post_id
         WHERE r.status IN ('confirmed','reclassified','downgraded')
         AND r.id=(SELECT MAX(r2.id) FROM reviews r2 WHERE r2.classification_id=c.id)
         AND NOT EXISTS (SELECT 1 FROM escalations e WHERE e.post_id=p.post_id AND e.resolved_at IS NULL)
         ORDER BY c.classified_at, c.id""")
        return rows + self.get_pending_escalations()

    def add_checkpoint(self, post_id, action, details):
        with self.conn:
            post_id = self._post_id(post_id)
            checkpoint_id = self._insert("checkpoints", {"post_id": post_id, "action": action, "details_json": json.dumps(details), "decided_at": _now()})
            if action in ("accept", "reclassify"):
                self.conn.execute("UPDATE escalations SET resolved_at=?, checkpoint_id=? WHERE post_id=? AND resolved_at IS NULL", (_now(), checkpoint_id, post_id))

    def apply_checkpoint(self, post_id, action, label, details, severity=None):
        """Atomically revise the decision and resolve its human-review queue entry."""
        with self.conn:
            post_id = self._post_id(post_id)
            if action not in ("accept", "reclassify"):
                raise ValueError("Unsupported checkpoint decision")
            c = self.get_classification(post_id)
            if c:
                c.update(label=label, confidence="high", severity=severity or c["severity"], unknown_terms_json=None)
                self._classification(c, c["run_id"])
            elif action == "reclassify":
                raise ValueError("Classify this post before reclassifying it at a checkpoint")
            # Accepting an unclassified escalation resolves only the human queue;
            # it must not invent a label/severity or count as a reviewed finding.
            checkpoint_id = self._insert("checkpoints", {"post_id": post_id, "action": action, "details_json": json.dumps(details), "decided_at": _now()})
            self.conn.execute("UPDATE escalations SET resolved_at=?, checkpoint_id=? WHERE post_id=? AND resolved_at IS NULL", (_now(), checkpoint_id, post_id))

    def summary(self):
        def count(q):
            return self.conn.execute(q).fetchone()[0]
        total = count("SELECT COUNT(*) FROM posts")
        classified = count("SELECT COUNT(*) FROM current_classifications")
        last = self._rows("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return {"total_posts": total, "classified": classified, "unclassified": total-classified,
                "flagged_low_confidence": count("SELECT COUNT(*) FROM current_classifications WHERE confidence='low'"),
                "investigated": count("SELECT COUNT(DISTINCT post_id) FROM investigations"),
                "reviewed": count("SELECT COUNT(DISTINCT c.post_id) FROM current_classifications c JOIN reviews r ON r.classification_id=c.id"),
                "unreviewed_medium_high": len(self.get_unreviewed_findings()),
                "pending_escalations": count("SELECT COUNT(*) FROM escalations WHERE resolved_at IS NULL"),
                "last_run": last[0] if last else None}
