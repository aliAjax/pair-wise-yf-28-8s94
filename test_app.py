import hashlib
import json
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path

from app import LEDGER_GENESIS, BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self.store.enroll("site1", self.trial["id"], f"S001-{i:03d}", {"risk": "low"})
            for i in range(1, 5)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")


class AuditLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "链测试研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-002"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def _enroll(self, i):
        return self.store.enroll("site1", self.trial["id"], f"L-{i:03d}", {"risk": "low"})

    def test_every_audit_event_appends_ledger_entry_and_verifies_intact(self):
        p = self._enroll(1)
        req = self.store.request_unblinding("site1", p["id"], "受试者发生严重不良事件需要紧急处理")
        self.store.approve_unblinding("monitor1", req["id"])
        self.store.approve_unblinding("monitor2", req["id"])
        with self.store.connect() as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE trial_id=?", (self.trial["id"],)
            ).fetchone()[0]
            ledger_count = conn.execute(
                "SELECT COUNT(*) FROM audit_ledger WHERE trial_id=?", (self.trial["id"],)
            ).fetchone()[0]
            seqs = [r["chain_seq"] for r in conn.execute(
                "SELECT chain_seq FROM audit_ledger WHERE trial_id=? ORDER BY chain_seq", (self.trial["id"],)
            ).fetchall()]
        self.assertEqual(ledger_count, audit_count)
        self.assertEqual(seqs, list(range(1, ledger_count + 1)))
        result = self.store.verify_chain("coord", self.trial["id"])
        self.assertEqual(result["status"], "intact")
        self.assertEqual(result["breakpoints"], [])
        self.assertIsNotNone(result["last_verified_at"])
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertEqual(summary["chain"]["status"], "intact")
        self.assertEqual(summary["chain"]["chain_length"], ledger_count)
        self.assertEqual(summary["chain"]["last_verified_at"], result["last_verified_at"])

    def test_ledger_hash_chain_is_cryptographically_correct(self):
        self._enroll(1)
        self._enroll(2)
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT l.chain_seq,l.prev_hash,l.entry_hash,l.audit_id,l.created_at,
                          a.actor_id,a.action,a.detail,a.trial_id
                   FROM audit_ledger l JOIN audit_log a ON a.id=l.audit_id
                   WHERE l.trial_id=? ORDER BY l.chain_seq""",
                (self.trial["id"],),
            ).fetchall()
        prev_hash = LEDGER_GENESIS
        for r in rows:
            self.assertEqual(r["prev_hash"], prev_hash)
            content = {
                "v": 1, "audit_id": r["audit_id"], "trial_id": r["trial_id"],
                "actor_id": r["actor_id"], "action": r["action"], "detail": r["detail"],
                "created_at": r["created_at"], "chain_seq": r["chain_seq"], "prev_hash": r["prev_hash"],
            }
            payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self.assertEqual(r["entry_hash"], hashlib.sha256(payload.encode("utf-8")).hexdigest())
            prev_hash = r["entry_hash"]

    def test_verify_detects_rewritten_audit_record(self):
        self._enroll(1)
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE audit_log SET detail=? WHERE trial_id=? AND action='participant.enroll'",
                (json.dumps({"tampered": True}, sort_keys=True), self.trial["id"]),
            )
        result = self.store.verify_chain("coord", self.trial["id"])
        self.assertEqual(result["status"], "broken")
        types = {b["type"] for b in result["breakpoints"]}
        self.assertIn("rewritten", types)

    def test_verify_detects_missing_chain_position(self):
        self._enroll(1)
        self._enroll(2)
        with self.store.connect() as conn:
            conn.execute("DELETE FROM audit_ledger WHERE trial_id=? AND chain_seq=2", (self.trial["id"],))
        result = self.store.verify_chain("coord", self.trial["id"])
        self.assertEqual(result["status"], "broken")
        types = {b["type"] for b in result["breakpoints"]}
        self.assertIn("missing", types)
        self.assertIn("omitted", types)

    def test_verify_detects_omitted_audit_not_in_chain(self):
        self._enroll(1)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(trial_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                (self.trial["id"], "site1", "participant.enroll", json.dumps({"fake": True}), "2026-01-01T00:00:00+00:00"),
            )
        result = self.store.verify_chain("coord", self.trial["id"])
        self.assertEqual(result["status"], "broken")
        types = {b["type"] for b in result["breakpoints"]}
        self.assertIn("omitted", types)

    def test_verify_detects_orphan_ledger_entry(self):
        self._enroll(1)
        with self.store.connect() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM audit_log WHERE trial_id=? AND action='participant.enroll'", (self.trial["id"],))
        result = self.store.verify_chain("coord", self.trial["id"])
        self.assertEqual(result["status"], "broken")
        types = {b["type"] for b in result["breakpoints"]}
        self.assertIn("orphan", types)

    def test_backfill_old_data_rebuilds_chain_in_order(self):
        self._enroll(1)
        self._enroll(2)
        with self.store.connect() as conn:
            conn.execute("DROP TABLE audit_ledger")
            conn.execute("DROP TABLE chain_verifications")
        self.store.init_schema()
        with self.store.connect() as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE trial_id=?", (self.trial["id"],)
            ).fetchone()[0]
            ledger_count = conn.execute(
                "SELECT COUNT(*) FROM audit_ledger WHERE trial_id=?", (self.trial["id"],)
            ).fetchone()[0]
            seqs = [r["chain_seq"] for r in conn.execute(
                "SELECT chain_seq FROM audit_ledger WHERE trial_id=? ORDER BY chain_seq", (self.trial["id"],)
            ).fetchall()]
        self.assertEqual(ledger_count, audit_count)
        self.assertEqual(seqs, list(range(1, audit_count + 1)))
        result = self.store.verify_chain("coord", self.trial["id"])
        self.assertEqual(result["status"], "intact")

    def test_concurrent_enrolls_do_not_write_same_chain_position(self):
        barrier = threading.Barrier(2)
        errors = []

        def enroll(i):
            try:
                barrier.wait()
                self.store.enroll("site1", self.trial["id"], f"C-{i:03d}", {"risk": "low"})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=enroll, args=(i,)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with self.store.connect() as conn:
            seqs = [r["chain_seq"] for r in conn.execute(
                """SELECT l.chain_seq FROM audit_ledger l JOIN audit_log a ON a.id=l.audit_id
                   WHERE l.trial_id=? AND a.action='participant.enroll' ORDER BY l.chain_seq""",
                (self.trial["id"],),
            ).fetchall()]
            participants = conn.execute(
                "SELECT COUNT(*) FROM participants WHERE trial_id=?", (self.trial["id"],)
            ).fetchone()[0]
        self.assertEqual(participants, 2)
        self.assertEqual(len(seqs), 2)
        self.assertEqual(len(set(seqs)), 2)

    def test_failed_append_leaves_no_half_chain_and_retry_succeeds(self):
        real_append = self.store._append_ledger
        state = {"failed": False}

        def failing_append(*args, **kwargs):
            if not state["failed"]:
                state["failed"] = True
                raise RuntimeError("simulated disk failure")
            return real_append(*args, **kwargs)

        self.store._append_ledger = failing_append
        with self.assertRaises(RuntimeError):
            self.store.enroll("site1", self.trial["id"], "F-001", {"risk": "low"})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants WHERE trial_id=?", (self.trial["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE trial_id=? AND action='participant.enroll'", (self.trial["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_ledger WHERE trial_id=?", (self.trial["id"],)).fetchone()[0], 2)
        self.store._append_ledger = real_append
        self.store.enroll("site1", self.trial["id"], "F-001", {"risk": "low"})
        with self.store.connect() as conn:
            seq = conn.execute(
                """SELECT l.chain_seq FROM audit_ledger l JOIN audit_log a ON a.id=l.audit_id
                   WHERE l.trial_id=? AND a.action='participant.enroll'""",
                (self.trial["id"],),
            ).fetchone()["chain_seq"]
        self.assertEqual(seq, 3)


if __name__ == "__main__":
    unittest.main()
