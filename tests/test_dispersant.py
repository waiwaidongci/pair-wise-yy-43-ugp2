import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service

VALID_FROM = "2026-01-01T00:00:00Z"
VALID_UNTIL = "2026-12-31T23:59:59Z"
START = "2026-06-01T08:00:00Z"
END = "2026-06-01T10:00:00Z"


class DispersantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "spill", "description": "dispersant ledger", "severity": "major",
             "quantity": 12, "threshold": 6, "external_ref": "OS-DISP-1"},
            "creator", "observer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _batch(self, total=1000.0, areas=("A1", "A2"), **kw):
        payload = {"batch_no": kw.get("batch_no", "B-001"), "approved_total": total,
                   "unit": "L", "allowed_areas": list(areas),
                   "valid_from": kw.get("valid_from", VALID_FROM),
                   "valid_until": kw.get("valid_until", VALID_UNTIL)}
        return self.service.create_batch(payload, kw.get("actor", "chemist"),
                                         kw.get("role", "response_commander"))

    def _spray(self, batch, amount=100.0, area="A1", item=None, **kw):
        payload = {"batch_id": batch["id"], "amount": amount,
                   "started_at": kw.get("started_at", START),
                   "ended_at": kw.get("ended_at", END),
                   "area": area, "latitude": kw.get("latitude", 30.0),
                   "longitude": kw.get("longitude", 122.0),
                   "vessel": kw.get("vessel", "东海救101"),
                   "external_ref": kw.get("external_ref")}
        return self.service.register_spray(
            (item or self.item)["id"], payload, kw.get("actor", "operator"),
            kw.get("role", "operations"))

    def test_batch_ledger_and_valid_spray(self):
        batch = self._batch()
        self.assertEqual(batch["remaining"], 1000.0)
        self.assertEqual(batch["used_total"], 0.0)
        spray = self._spray(batch)
        self.assertEqual(spray["status"], "active")
        self.assertEqual(spray["remaining"], 900.0)
        usage = self.service.batch_usage(batch["id"], "viewer")
        self.assertEqual(len(usage["usages"]), 1)
        self.assertEqual(usage["active_count"], 1)
        self.assertAlmostEqual(usage["used_total"], 100.0)
        self.assertAlmostEqual(usage["remaining"], 900.0)

    def test_expired_batch_blocks_whole_order(self):
        batch = self._batch(valid_until="2026-05-31T23:59:59Z")
        before = self.service.batch_usage(batch["id"], "viewer")
        with self.assertRaises(ConflictError) as ctx:
            self._spray(batch)
        codes = [c["code"] for c in ctx.exception.details["conflicts"]]
        self.assertIn("batch_expired", codes)
        self.assertEqual(ctx.exception.details["batch_no"], "B-001")
        # 整单不保存
        after = self.service.batch_usage(batch["id"], "viewer")
        self.assertEqual(after["active_count"], before["active_count"])
        self.assertEqual(after["used_total"], 0.0)

    def test_not_yet_effective_batch(self):
        batch = self._batch(valid_from="2026-07-01T00:00:00Z")
        with self.assertRaises(ConflictError) as ctx:
            self._spray(batch)
        self.assertIn("batch_not_effective",
                      [c["code"] for c in ctx.exception.details["conflicts"]])

    def test_area_mismatch_blocks(self):
        batch = self._batch(areas=("A1",))
        with self.assertRaises(ConflictError) as ctx:
            self._spray(batch, area="A9")
        codes = [c["code"] for c in ctx.exception.details["conflicts"]]
        self.assertIn("area_mismatch", codes)
        self.assertEqual(self.service.list_sprays("viewer", batch_id=batch["id"]), [])

    def test_over_quota_blocks_and_reports_remaining(self):
        batch = self._batch(total=150.0)
        self._spray(batch, amount=100.0)
        with self.assertRaises(ConflictError) as ctx:
            self._spray(batch, amount=60.0)
        codes = [c["code"] for c in ctx.exception.details["conflicts"]]
        self.assertIn("over_quota", codes)
        self.assertEqual(ctx.exception.details["remaining"], 50.0)
        # 已登记的100仍在，冲突单未保存
        usage = self.service.batch_usage(batch["id"], "viewer")
        self.assertEqual(usage["active_count"], 1)
        self.assertAlmostEqual(usage["used_total"], 100.0)

    def test_withdraw_keeps_trace_but_releases_quota(self):
        batch = self._batch(total=150.0)
        spray = self._spray(batch, amount=100.0)
        # 撤回后余量恢复，可以再喷此前会超限的量
        result = self.service.withdraw_spray(
            spray["id"], {"reason": "记录有误，口头登记撤销"}, "operator", "operations")
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["remaining"], 150.0)
        again = self._spray(batch, amount=150.0)
        self.assertEqual(again["status"], "active")
        usage = self.service.batch_usage(batch["id"], "viewer")
        self.assertEqual(usage["active_count"], 1)
        self.assertEqual(usage["withdrawn_count"], 1)
        # 撤回留下过程
        self.assertEqual(len(usage["usages"]), 2)
        with self.assertRaises(ConflictError):
            self.service.withdraw_spray(spray["id"], {"reason": "再次撤回"},
                                        "operator", "operations")

    def test_batch_correction_recalculates_open_items(self):
        batch = self._batch(total=1000.0, areas=("A1", "A2"))
        self._spray(batch, amount=800.0, area="A1")
        result = self.service.correct_batch(batch["id"], {
            "approved_total": 500.0, "expected_version": batch["version"],
        }, "chemist", "response_commander")
        self.assertEqual(result["version"], batch["version"] + 1)
        recalc = result["recalculation"]
        self.assertEqual(recalc["remaining"], -300.0)
        self.assertEqual(len(recalc["conflicts"]), 1)
        self.assertIn("over_quota", recalc["conflicts"][0]["codes"])
        # 批次更正后未结束事件按新值核算：新喷洒被拒
        with self.assertRaises(ConflictError):
            self._spray(batch, amount=10.0, area="A1")

    def test_correction_area_and_expiry_rechecked(self):
        batch = self._batch()
        self._spray(batch, area="A1")
        result = self.service.correct_batch(batch["id"], {
            "allowed_areas": ["A5"],
        }, "chemist", "response_commander")
        recalc = result["recalculation"]
        self.assertIn("area_mismatch", recalc["conflicts"][0]["codes"])
        result2 = self.service.correct_batch(batch["id"], {
            "valid_until": "2026-01-31T00:00:00Z",
        }, "chemist", "response_commander")
        codes = result2["recalculation"]["conflicts"][0]["codes"]
        self.assertIn("batch_expired", codes)

    def test_closed_item_blocks_register_and_withdraw(self):
        from src.rules import STATES, TRANSITION_ROLES
        batch = self._batch()
        spray = self._spray(batch)
        current = self.service.get_item(self.item["id"], "viewer")
        self.service.add_record(current["id"], {"kind": "evidence",
            "detail": "回收和岸线监测完成", "status": "closed"},
            "recorder", "response_commander")
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "closed")
        with self.assertRaises(ConflictError):
            self._spray(batch)
        with self.assertRaises(ConflictError):
            self.service.withdraw_spray(spray["id"], {"reason": "关闭后撤回"},
                                        "operator", "operations")

    def test_closed_item_consumption_reserved_on_correction(self):
        from src.rules import STATES, TRANSITION_ROLES
        batch = self._batch(total=1000.0)
        # 事件1：喷洒600后关闭，额度永久占用
        self._spray(batch, amount=600.0)
        current = self.service.get_item(self.item["id"], "viewer")
        self.service.add_record(current["id"], {"kind": "evidence",
            "detail": "done", "status": "closed"}, "recorder", "response_commander")
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        # 事件2（未结束）：喷洒300
        item2 = self.service.create_item(
            {"title": "spill2", "description": "open", "severity": "minor",
             "quantity": 1, "threshold": 1}, "creator", "observer")
        self._spray(batch, amount=300.0, item=item2)
        # 更正到800：已用900 -> 余量-100；关闭事件占用600后池子200，事件2的300超量
        result = self.service.correct_batch(batch["id"], {"approved_total": 800.0},
                                            "chemist", "response_commander")
        recalc = result["recalculation"]
        self.assertEqual(recalc["remaining"], -100.0)
        self.assertEqual(len(recalc["conflicts"]), 1)
        conflict = recalc["conflicts"][0]
        self.assertEqual(conflict["item_id"], item2["id"])
        self.assertIn("over_quota", conflict["codes"])

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self._batch(role="operations")
        with self.assertRaises(PermissionDenied):
            self._batch(role="viewer")
        batch = self._batch()
        with self.assertRaises(PermissionDenied):
            self._spray(batch, role="viewer")
        with self.assertRaises(PermissionDenied):
            self.service.correct_batch(batch["id"], {"approved_total": 1.0},
                                       "chemist", "operations")
        # 只读角色可以查
        self.assertEqual(self.service.list_batches("viewer")[0]["batch_no"], "B-001")

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.create_batch(
                {"batch_no": "B-X", "approved_total": 0, "allowed_areas": ["A1"],
                 "valid_until": VALID_UNTIL}, "chemist", "response_commander")
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self._spray(batch, latitude=200.0)
        with self.assertRaises(ValidationError):
            self._spray(batch, started_at="not-a-time")
        with self.assertRaises(ValidationError):
            self._spray(batch, started_at=END, ended_at=START)

    def test_spray_and_withdraw_enter_audit_chain(self):
        batch = self._batch()
        spray = self._spray(batch)
        self.service.withdraw_spray(spray["id"], {"reason": "误录"},
                                    "operator", "operations")
        self.service.correct_batch(batch["id"], {"approved_total": 1200.0},
                                   "chemist", "response_commander")
        events = self.service.audit("viewer")
        actions = [e["action"] for e in events]
        self.assertIn("batch_create", actions)
        self.assertIn("spray_register", actions)
        self.assertIn("spray_withdraw", actions)
        self.assertIn("batch_correct", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_list_usage_by_batch(self):
        b1 = self._batch(batch_no="B-001")
        b2 = self._batch(batch_no="B-002")
        self._spray(b1, amount=10.0)
        self._spray(b2, amount=20.0)
        b1_usage = self.service.list_sprays("viewer", batch_id=b1["id"])
        self.assertEqual(len(b1_usage), 1)
        self.assertEqual(b1_usage[0]["batch_no"], "B-001")
        item_usage = self.service.list_sprays("viewer", item_id=self.item["id"])
        self.assertEqual(len(item_usage), 2)


if __name__ == "__main__":
    unittest.main()
