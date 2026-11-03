import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.state import State, internal_post_id
from lib.checkpoint import run_checkpoint


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.state = State(self.path)
        self.run = self.state.start_run()
        self.state.add_posts([{"post_id": "7", "source": "forum", "text": "original evidence"}])
        self.pid = internal_post_id("forum", "7")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def classify(self, label="harm", confidence="high"):
        self.state.add_classifications([{"post_id": self.pid, "label": label, "confidence": confidence, "severity": "high"}], self.run)
        return self.state._current(self.pid)["id"]

    def review(self, status="confirmed", **kwargs):
        self.state.add_reviews([dict(post_id=self.pid, status=status, classification_id=self.state._current(self.pid)["id"], **kwargs)], self.run)

    def test_source_local_ids_do_not_collide(self):
        self.state.add_posts([{"post_id": "7", "source": "other", "text": "different evidence"}])
        self.assertEqual(self.state.summary()["total_posts"], 2)
        with self.assertRaises(ValueError):
            self.state.get_post("7")
        self.assertEqual(self.state.get_post(self.pid)["source_post_id"], "7")
        self.assertNotEqual(internal_post_id("a:b", "c"), internal_post_id("a", "b:c"))
        self.state.add_posts([{"post_id": "7", "source": "forum", "text": "duplicate"}])
        self.assertEqual(self.state.get_post(self.pid)["text"], "original evidence")

    def test_reclassification_invalidates_review(self):
        original = self.classify()
        self.review()
        self.assertEqual(len(self.state.get_reviewed_findings()), 1)
        self.state.update_classification(self.pid, "new", "high")
        self.assertEqual(len(self.state.get_reviewed_findings()), 0)
        self.assertEqual(len(self.state.get_unreviewed_findings()), 1)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            self.state.add_reviews([{"post_id": self.pid, "status": "confirmed", "classification_id": original}], self.run)

    def test_reviewer_revision_is_bound_to_result(self):
        original = self.classify()
        self.review("reclassified", revised_label="revised", revised_severity="medium")
        result = self.state.get_reviewed_findings()[0]
        self.assertNotEqual(result["classification_id"], original)
        self.assertEqual(result["classification_label"], "revised")
        self.assertEqual(result["severity"], "medium")
        self.assertEqual(self.state.get_unreviewed_findings(), [])

    def test_escalation_survives_reclassification_and_restart(self):
        self.classify()
        self.review("escalated", reason="needs person")
        self.classify("new")
        self.review()
        self.state.close()
        self.state = State(self.path)
        self.assertEqual(self.state.summary()["pending_escalations"], 1)
        self.assertEqual(self.state.get_reviewed_findings()[0]["status"], "escalated")
        self.assertTrue(self.state.get_flagged_classifications()[0]["escalation_id"])
        with patch("lib.checkpoint._prompt", return_value="s"):
            run_checkpoint(self.state.get_flagged_classifications(), self.state, self.path)
        self.assertEqual(self.state.summary()["pending_escalations"], 1)
        with patch("lib.checkpoint._prompt", return_value="a"):
            run_checkpoint(self.state.get_flagged_classifications(), self.state, self.path)
        self.assertEqual(self.state.summary()["pending_escalations"], 0)
        self.assertEqual(self.state.get_reviewed_findings(), [])

    def test_investigator_escalation_persists(self):
        self.classify()
        self.state.add_escalation(self.pid, "check context", self.run)
        self.assertEqual(self.state.get_flagged_classifications()[0]["escalation_reason"], "check context")

    def test_unclassified_escalation_can_be_accepted_without_inventing_classification(self):
        self.assertIsNone(self.state.get_classification(self.pid))
        self.state.add_escalation(self.pid, "check manually")
        self.assertEqual(self.state.get_flagged_classifications()[0]["post_id"], self.pid)
        self.state.apply_checkpoint(self.pid, "accept", None, {})
        self.assertEqual(self.state.summary()["pending_escalations"], 0)
        self.assertIsNone(self.state.get_classification(self.pid))
        self.assertEqual(self.state.get_reviewed_findings(), [])

    def test_stale_second_review_rolls_back_entire_batch(self):
        original = self.classify()
        self.state.add_posts([{"post_id": "8", "source": "forum", "text": "second"}])
        second = internal_post_id("forum", "8")
        self.state.add_classifications([{"post_id": second, "label": "harm", "confidence": "high", "severity": "high"}], self.run)
        with self.assertRaises(ValueError):
            self.state.add_reviews([
                {"post_id": self.pid, "classification_id": original, "status": "reclassified", "revised_label": "new"},
                {"post_id": second, "classification_id": original, "status": "confirmed"},
            ], self.run)
        self.assertEqual(self.state.get_classification(self.pid)["id"], original)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)

    def test_investigation_reclassification_and_escalation_are_atomic(self):
        investigation = {"post_id": self.pid, "recommendation": "reclassify",
                         "revised_label": "new", "confidence": "high", "thread_context_summary": "context"}
        with self.assertRaises(ValueError):
            self.state.apply_investigation(investigation, self.run)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0], 0)
        self.classify()
        self.review()
        self.state.apply_investigation(investigation, self.run)
        self.assertEqual(self.state.get_classification(self.pid)["label"], "new")
        self.assertEqual(self.state.get_reviewed_findings(), [])
        investigation["recommendation"] = "needs_human"
        self.state.apply_investigation(investigation, self.run)
        self.assertEqual(self.state.summary()["pending_escalations"], 1)
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0], 2)

    def test_batch_writes_are_atomic_and_foreign_keys_enforced(self):
        with self.assertRaises(KeyError):
            self.state.add_posts([{"post_id": "8", "source": "forum", "text": "valid"}, {"post_id": "9", "source": "forum"}])
        self.assertEqual(self.state.summary()["total_posts"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.add_classifications([{"post_id": self.pid, "label": "x", "confidence": "high", "severity": "high"}], 999)
        self.assertEqual(self.state.summary()["classified"], 0)
        self.classify()
        with self.assertRaises(ValueError):
            self.state.add_reviews([{"post_id": self.pid, "status": "reclassified", "revised_label": "new"}, {"post_id": "missing", "status": "confirmed"}], self.run)
        self.assertEqual(self.state._current(self.pid)["label"], "harm")
        self.assertEqual(self.state.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)
        self.assertEqual(self.state.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)


class MigrationTests(unittest.TestCase):
    def test_legacy_reviews_are_not_assumed_current_and_orphans_are_archived(self):
        # Minimal legacy layout, independent of git availability and external services.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".samovar").mkdir()
            conn = sqlite3.connect(root / ".samovar/state.db")
            conn.executescript('''
             CREATE TABLE posts (post_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_language TEXT,
               text TEXT NOT NULL, url TEXT, thread_url TEXT, source_ts TEXT, collected_at TEXT NOT NULL, metadata_json TEXT);
             CREATE TABLE classifications (id INTEGER PRIMARY KEY, post_id TEXT, label TEXT, severity TEXT,
               confidence TEXT, evidence_en TEXT, unknown_terms_json TEXT, run_id INTEGER, classified_at TEXT);
             CREATE UNIQUE INDEX idx_classifications_post_id ON classifications(post_id);
             CREATE TABLE reviews (id INTEGER PRIMARY KEY, post_id TEXT, status TEXT, original_label TEXT,
               original_severity TEXT, revised_label TEXT, revised_severity TEXT, reason TEXT, run_id INTEGER, reviewed_at TEXT);
             INSERT INTO posts VALUES ('7','forum',NULL,'legacy evidence',NULL,NULL,NULL,'2020',NULL);
             INSERT INTO classifications VALUES (1,'7','harm','high','high',NULL,NULL,NULL,'2020');
             INSERT INTO reviews VALUES (1,'7','confirmed','harm','high',NULL,NULL,NULL,NULL,'2020');
             INSERT INTO reviews VALUES (2,'7','escalated','harm','high',NULL,NULL,'pending',NULL,'2020');
             INSERT INTO reviews VALUES (3,'missing','confirmed','harm','high',NULL,NULL,NULL,NULL,'2020');
            ''')
            conn.close()
            state = State(root)
            self.assertEqual(state.get_post('7')['post_id'], internal_post_id('forum', '7'))
            self.assertEqual(len(state.get_unreviewed_findings()), 1)
            self.assertEqual(state.get_reviewed_findings()[0]['status'], 'escalated')
            self.assertEqual(state.conn.execute('SELECT COUNT(*) FROM migration_archive').fetchone()[0], 1)
            self.assertEqual(state.conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            state.close()
            state = State(root)
            self.assertEqual(state.summary()['pending_escalations'], 1)
            self.assertEqual(state.summary()['total_posts'], 1)
            state.close()

    def test_failed_migration_leaves_legacy_tables_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.samovar').mkdir()
            db = root / '.samovar/state.db'
            conn = sqlite3.connect(db)
            conn.executescript("CREATE TABLE posts (post_id TEXT, source TEXT, text TEXT, collected_at TEXT); INSERT INTO posts VALUES ('7',NULL,'evidence','2020');")
            conn.close()
            with self.assertRaises(sqlite3.IntegrityError):
                State(root)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute('SELECT text FROM posts').fetchone()[0], 'evidence')
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            conn.close()


if __name__ == '__main__':
    unittest.main()
