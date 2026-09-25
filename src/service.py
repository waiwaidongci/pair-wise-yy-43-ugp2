from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ValidationError, ensure_role, normalize_severity,
                     require_coordinate, require_number, require_text,
                     require_text_list, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_ENTITY, BATCH_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, SPRAY_ROLES, TITLE, USAGE_ENTITY, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    remaining_quota, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def register_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        approved_total = require_number(payload.get("approved_total"), "approved_total", 0.000001)
        sea_areas = require_text_list(payload.get("sea_areas"), "sea_areas")
        valid_from = require_timestamp(payload.get("valid_from"), "valid_from")
        valid_until = require_timestamp(payload.get("valid_until"), "valid_until")
        if valid_from >= valid_until:
            raise ValidationError("有效期起点必须早于有效期终点")
        batch = self.repository.create_batch(batch_no, approved_total, sea_areas,
                                             valid_from, valid_until, actor)
        self.repository.append_audit("batch_register", BATCH_ENTITY, batch["id"], actor, {
            "batch_no": batch_no, "approved_total": approved_total,
            "sea_areas": sea_areas, "valid_from": valid_from, "valid_until": valid_until,
        })
        return self.enrich_batch(batch)

    def correct_batch(self, batch_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        before = self.repository.get_batch(batch_id)
        approved_total = require_number(
            payload.get("approved_total", before["approved_total"]), "approved_total", 0.000001)
        sea_areas = require_text_list(payload.get("sea_areas", before["sea_areas"]), "sea_areas")
        valid_from = require_timestamp(payload.get("valid_from", before["valid_from"]), "valid_from")
        valid_until = require_timestamp(payload.get("valid_until", before["valid_until"]), "valid_until")
        if valid_from >= valid_until:
            raise ValidationError("有效期起点必须早于有效期终点")
        batch = self.repository.correct_batch(batch_id, approved_total, sea_areas,
                                              valid_from, valid_until, actor)
        self.repository.append_audit("batch_correct", BATCH_ENTITY, batch_id, actor, {
            "batch_no": batch["batch_no"],
            "before": {"approved_total": before["approved_total"],
                       "sea_areas": before["sea_areas"],
                       "valid_from": before["valid_from"],
                       "valid_until": before["valid_until"]},
            "after": {"approved_total": approved_total, "sea_areas": sea_areas,
                      "valid_from": valid_from, "valid_until": valid_until},
        })
        return self.enrich_batch(batch)

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_batch(self.repository.get_batch(batch_id))

    def list_batches(self, role: str) -> list:
        self._view(role)
        return [self.enrich_batch(batch) for batch in self.repository.list_batches()]

    def register_spray(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, SPRAY_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        quantity = require_number(payload.get("quantity"), "quantity", 0.000001)
        start_time = require_timestamp(payload.get("start_time"), "start_time")
        end_time = require_timestamp(payload.get("end_time"), "end_time")
        if start_time > end_time:
            raise ValidationError("开始时间不能晚于结束时间")
        latitude = require_coordinate(payload.get("latitude"), "latitude", -90.0, 90.0)
        longitude = require_coordinate(payload.get("longitude"), "longitude", -180.0, 180.0)
        sea_area = require_text(payload.get("sea_area"), "sea_area", 100)
        vessel = require_text(payload.get("vessel"), "vessel", 100)
        usage = self.repository.register_usage(item_id, batch_no, quantity, start_time,
                                               end_time, latitude, longitude, sea_area,
                                               vessel, actor)
        batch = self.repository.get_batch(usage["batch_id"])
        remaining = remaining_quota(batch["approved_total"],
                                    self.repository.active_usage_total(batch["id"]))
        self.repository.append_audit("spray", USAGE_ENTITY, usage["id"], actor, {
            "item_id": item_id, "batch_no": batch["batch_no"], "quantity": quantity,
            "start_time": start_time, "end_time": end_time, "sea_area": sea_area,
            "latitude": latitude, "longitude": longitude, "vessel": vessel,
            "remaining": remaining,
        })
        result = dict(usage, batch_no=batch["batch_no"], remaining=remaining)
        return result

    def withdraw_spray(self, usage_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, SPRAY_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = require_text(payload.get("reason"), "reason")
        usage = self.repository.withdraw_usage(usage_id, reason, actor)
        batch = self.repository.get_batch(usage["batch_id"])
        remaining = remaining_quota(batch["approved_total"],
                                    self.repository.active_usage_total(batch["id"]))
        self.repository.append_audit("withdraw", USAGE_ENTITY, usage_id, actor, {
            "item_id": usage["item_id"], "batch_no": batch["batch_no"],
            "quantity": usage["quantity"], "reason": reason, "remaining": remaining,
        })
        result = dict(usage, batch_no=batch["batch_no"], remaining=remaining)
        return result

    def list_sprays(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_usages_by_item(item_id)

    def batch_usage(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_batch(batch_id)
        used = self.repository.active_usage_total(batch_id)
        return {
            "batch": self.enrich_batch(batch),
            "usages": self.repository.list_usages_by_batch(batch_id),
            "used_total": used,
            "remaining": remaining_quota(batch["approved_total"], used),
        }

    def enrich_batch(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(batch)
        used = self.repository.active_usage_total(batch["id"])
        result["used_total"] = used
        result["remaining"] = remaining_quota(batch["approved_total"], used)
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
