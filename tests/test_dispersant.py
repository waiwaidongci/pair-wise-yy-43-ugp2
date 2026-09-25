import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service

BATCH = {"batch_no": "DSP-2026-001", "approved_total": 100,
         "sea_areas": ["南海北部", "珠江口"],
         "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2026-12-31T23:59:59Z"}
SPRAY = {"batch_no": "DSP-2026-001", "quantity": 30,
         "start_time": "2026-06-01T08:00:00Z", "end_time": "2026-06-01T10:00:00Z",
         "latitude": 22.1, "longitude": 113.5, "sea_area": "南海北部", "vessel": "海巡01"}

class DispersantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "泄漏事件", "description": "dispersant ledger", "severity": "major",
             "quantity": 12, "threshold": 6, "external_ref": "DSP-ITEM-1"}, "creator", "observer")
        self.batch = self.service.register_batch(dict(BATCH), "commander", "response_commander")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def spray(self, **overrides):
        payload = dict(SPRAY); payload.update(overrides)
        return self.service.register_spray(self.item["id"], payload, "sprayer", "operations")

    def test_register_spray_and_audit(self):
        usage = self.spray()
        self.assertEqual(usage["status"], "active")
        self.assertEqual(usage["remaining"], 70)
        ledger = self.service.list_sprays(self.item["id"], "viewer")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["batch_no"], "DSP-2026-001")
        self.assertEqual(ledger[0]["vessel"], "海巡01")
        view = self.service.batch_usage(self.batch["id"], "viewer")
        self.assertEqual(view["used_total"], 30)
        self.assertEqual(view["remaining"], 70)
        actions = [e["action"] for e in self.service.audit("viewer")]
        self.assertIn("batch_register", actions)
        self.assertIn("spray", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_expired_batch_rejected_with_batch_and_remaining(self):
        with self.assertRaises(ConflictError) as ctx:
            self.spray(start_time="2027-01-01T08:00:00Z", end_time="2027-01-01T10:00:00Z")
        message = str(ctx.exception)
        self.assertIn("DSP-2026-001", message)
        self.assertIn("100", message)
        self.assertEqual(self.service.list_sprays(self.item["id"], "viewer"), [])

    def test_wrong_sea_area_rejected(self):
        with self.assertRaises(ConflictError) as ctx:
            self.spray(sea_area="东海")
        message = str(ctx.exception)
        self.assertIn("DSP-2026-001", message)
        self.assertIn("东海", message)
        self.assertEqual(self.service.list_sprays(self.item["id"], "viewer"), [])

    def test_over_quota_rejected_then_withdrawal_frees_quota(self):
        self.spray(quantity=80)
        with self.assertRaises(ConflictError) as ctx:
            self.spray(quantity=30)
        message = str(ctx.exception)
        self.assertIn("DSP-2026-001", message)
        self.assertIn("20", message)
        self.assertEqual(len(self.service.list_sprays(self.item["id"], "viewer")), 1)
        usage = self.service.list_sprays(self.item["id"], "viewer")[0]
        withdrawn = self.service.withdraw_spray(usage["id"], {"reason": "坐标录错"}, "checker", "operations")
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["remaining"], 100)
        ledger = self.service.list_sprays(self.item["id"], "viewer")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["status"], "withdrawn")
        self.assertEqual(ledger[0]["withdraw_reason"], "坐标录错")
        again = self.spray(quantity=90)
        self.assertEqual(again["remaining"], 10)
        actions = [e["action"] for e in self.service.audit("viewer")]
        self.assertIn("withdraw", actions)

    def test_batch_correction_reaccounts_open_events(self):
        self.spray(quantity=90)
        with self.assertRaises(ConflictError):
            self.spray(quantity=20)
        corrected = self.service.correct_batch(
            self.batch["id"], {"approved_total": 150}, "commander", "response_commander")
        self.assertEqual(corrected["remaining"], 60)
        usage = self.spray(quantity=20)
        self.assertEqual(usage["remaining"], 40)
        with self.assertRaises(ConflictError):
            self.spray(sea_area="东海")
        self.service.correct_batch(
            self.batch["id"], {"sea_areas": ["南海北部", "珠江口", "东海"]},
            "commander", "response_commander")
        self.assertEqual(self.spray(quantity=10, sea_area="东海")["remaining"], 30)
        actions = [e["action"] for e in self.service.audit("viewer")]
        self.assertEqual(actions.count("batch_correct"), 2)

    def test_validation_and_permission_failures(self):
        with self.assertRaises(ValidationError):
            self.spray(quantity=0)
        with self.assertRaises(ValidationError):
            self.spray(start_time="2026-06-01T10:00:00Z", end_time="2026-06-01T08:00:00Z")
        with self.assertRaises(ValidationError):
            self.spray(latitude=91)
        with self.assertRaises(ValidationError):
            self.spray(start_time="not-a-time")
        with self.assertRaises(NotFoundError):
            self.spray(batch_no="NO-SUCH-BATCH")
        with self.assertRaises(PermissionDenied):
            self.service.register_spray(self.item["id"], dict(SPRAY), "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.register_batch(dict(BATCH, batch_no="DSP-2"), "x", "operations")
        with self.assertRaises(ConflictError):
            self.service.register_batch(dict(BATCH), "commander", "response_commander")
        usage = self.spray()
        self.service.withdraw_spray(usage["id"], {"reason": "重复录入"}, "checker", "operations")
        with self.assertRaises(ConflictError):
            self.service.withdraw_spray(usage["id"], {"reason": "再次撤回"}, "checker", "operations")

if __name__ == "__main__":
    unittest.main()
