import json
import sqlite3
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path

from app import GENESIS_PREV, BusinessError, RandomizationStore


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


class AuditChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "链式审计研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-002"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def _seqs(self):
        with self.store.connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT seq FROM audit_log WHERE trial_id=? ORDER BY seq", (self.trial["id"],)
            ).fetchall()]

    def test_appends_in_order_and_summary_shows_chain_status(self):
        participant = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        request = self.store.request_unblinding("site1", participant["id"], "受试者发生严重不良事件需要紧急处理")
        self.store.approve_unblinding("monitor1", request["id"])
        self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(self._seqs(), [1, 2, 3, 4, 5, 6])
        result = self.store.verify_audit_chain("monitor1", self.trial["id"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checked_entries"], 6)
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertEqual(summary["chain"]["entries"], 6)
        self.assertEqual(summary["chain"]["last_verify_status"], "ok")
        self.assertIsNotNone(summary["chain"]["last_verified_at"])
        self.assertIsNotNone(summary["chain"]["head_digest"])
        self.assertEqual(summary["chain"]["legacy_unsealed"], 0)

    def test_verify_detects_tampered_entry(self):
        self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE audit_log SET detail=? WHERE trial_id=? AND seq=2",
                (json.dumps({"forged": True}), self.trial["id"]),
            )
        result = self.store.verify_audit_chain("monitor1", self.trial["id"])
        self.assertEqual(result["status"], "failed")
        tampered = [i for i in result["issues"] if i["kind"] == "tampered"]
        self.assertEqual(len(tampered), 1)
        self.assertIn("seq=2", tampered[0]["detail"])
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertEqual(summary["chain"]["last_verify_status"], "failed")

    def test_verify_detects_deleted_entry(self):
        self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        with self.store.connect() as conn:
            conn.execute("DELETE FROM audit_log WHERE trial_id=? AND seq=2", (self.trial["id"],))
        result = self.store.verify_audit_chain("monitor1", self.trial["id"])
        self.assertEqual(result["status"], "failed")
        kinds = {i["kind"] for i in result["issues"]}
        self.assertIn("gap", kinds)
        self.assertIn("broken_link", kinds)

    def test_verify_detects_deleted_tail(self):
        self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        with self.store.connect() as conn:
            conn.execute("DELETE FROM audit_log WHERE trial_id=? AND seq=3", (self.trial["id"],))
        result = self.store.verify_audit_chain("monitor1", self.trial["id"])
        self.assertEqual(result["status"], "failed")
        kinds = {i["kind"] for i in result["issues"]}
        self.assertIn("tail_tampered", kinds)

    def test_verify_requires_privileged_role(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.verify_audit_chain("site1", self.trial["id"])
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_concurrent_enrolls_never_share_chain_position(self):
        barrier = threading.Barrier(3)
        results, errors = [], []

        def enroll(user, external_id):
            try:
                barrier.wait(10)
                results.append(self.store.enroll(user, self.trial["id"], external_id, {"risk": "low"}))
            except Exception as exc:  # noqa: BLE001 - 测试需要收集任意失败
                errors.append(exc)

        threads = [
            threading.Thread(target=enroll, args=("site1", "S001-100")),
            threading.Thread(target=enroll, args=("site2", "S002-100")),
        ]
        for t in threads:
            t.start()
        barrier.wait(10)
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(len({r["id"] for r in results}), 2)
        self.assertEqual(self._seqs(), [1, 2, 3, 4])
        self.assertEqual(self.store.verify_audit_chain("coord", self.trial["id"])["status"], "ok")

    def test_failed_write_leaves_no_partial_chain(self):
        before = self._seqs()
        with self.assertRaises(BusinessError):
            self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": ""})
        self.assertEqual(self._seqs(), before)
        # 模拟写盘失败：审计条目已插入但事务整体回滚
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.store._audit(conn, self.trial["id"], "coord", "test.half", {"x": 1})
            conn.rollback()
        self.assertEqual(self._seqs(), before)
        # 重试从同一链位继续，不留半条链
        self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.assertEqual(self._seqs(), before + [len(before) + 1])
        self.assertEqual(self.store.verify_audit_chain("coord", self.trial["id"])["status"], "ok")


class LegacyBackfillTests(unittest.TestCase):
    def test_legacy_rows_backfilled_in_existing_order(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                               site_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id INTEGER,
                actor_id TEXT NOT NULL, action TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO users VALUES('monitor1','监查员','monitor','CENTER',1)")
        conn.executemany(
            "INSERT INTO audit_log(trial_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            [
                (1, "coord", "trial.create", '{"a": 1}', "2026-01-01T00:00:00+00:00"),
                (1, "coord", "trial.start", "{}", "2026-01-02T00:00:00+00:00"),
                (None, "coord", "system.note", "{}", "2026-01-03T00:00:00+00:00"),
                (1, "site1", "participant.enroll", '{"p": 1}', "2026-01-04T00:00:00+00:00"),
            ],
        )
        conn.commit()
        conn.close()

        store = RandomizationStore(db)
        store.init_schema()  # 旧库升级：补列并按既有 id 顺序回填摘要
        with store.connect() as c:
            chained = c.execute(
                "SELECT seq, prev_digest, digest FROM audit_log WHERE trial_id=1 ORDER BY id"
            ).fetchall()
            self.assertEqual([r["seq"] for r in chained], [1, 2, 3])
            self.assertEqual(chained[0]["prev_digest"], GENESIS_PREV)
            self.assertEqual(chained[1]["prev_digest"], chained[0]["digest"])
            self.assertEqual(chained[2]["prev_digest"], chained[1]["digest"])
            global_row = c.execute("SELECT seq, prev_digest FROM audit_log WHERE trial_id IS NULL").fetchone()
            self.assertEqual((global_row["seq"], global_row["prev_digest"]), (1, GENESIS_PREV))
            self.assertIsNotNone(
                c.execute("SELECT backfilled_at FROM audit_chain_meta WHERE trial_id=1").fetchone()["backfilled_at"]
            )
        result = store.verify_audit_chain("monitor1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checked_entries"], 4)

        # 重复升级保持已有摘要不变
        with store.connect() as c:
            before = [tuple(r) for r in c.execute("SELECT seq, prev_digest, digest FROM audit_log ORDER BY id")]
        store.init_schema()
        with store.connect() as c:
            after = [tuple(r) for r in c.execute("SELECT seq, prev_digest, digest FROM audit_log ORDER BY id")]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
