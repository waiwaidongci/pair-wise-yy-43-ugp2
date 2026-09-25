from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role, normalize_iso,
                     normalize_severity, parse_datetime, require_area_list,
                     require_datetime, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_CORRECT_ROLES, BATCH_CREATE_ROLES,
                    BATCH_ENTITY, CREATE_ROLES, ENTITY, RECORD_ROLES, SPRAY_ENTITY,
                    SPRAY_ROLES, VIEW_ROLES, completion_blockers,
                    escalation_required, evaluate_spray, priority_score,
                    response_deadline_hours, role_for_transition, validate_transition)


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

    # ---- 消油剂批次台账 ----
    def _batch_with_dt(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(batch)
        result["valid_from_dt"] = (parse_datetime(batch["valid_from"])
                                   if batch.get("valid_from") else None)
        result["valid_until_dt"] = parse_datetime(batch["valid_until"])
        return result

    def create_batch(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        approved_total = require_number(payload.get("approved_total"), "approved_total", 0.000001)
        unit = require_text(payload.get("unit", "L"), "unit", 10)
        allowed_areas = require_area_list(payload.get("allowed_areas"))
        valid_from = payload.get("valid_from")
        valid_from_dt = parse_datetime(valid_from) if valid_from else None
        valid_until_dt = require_datetime(payload.get("valid_until"), "valid_until")
        if valid_from_dt is not None:
            valid_from = normalize_iso(valid_from_dt)
        else:
            valid_from = None
        valid_until = normalize_iso(valid_until_dt)
        if valid_from_dt is not None and valid_from_dt > valid_until_dt:
            raise ConflictError("生效时间不能晚于有效期截止时间")
        batch = self.repository.create_batch(batch_no, approved_total, unit,
                                             allowed_areas, valid_from, valid_until, actor)
        self.repository.append_audit("batch_create", BATCH_ENTITY, batch["id"], actor, {
            "batch_no": batch_no, "approved_total": approved_total, "unit": unit,
            "allowed_areas": allowed_areas, "valid_from": valid_from,
            "valid_until": valid_until,
        })
        return self.enrich_batch(batch)

    def list_batches(self, role: str) -> list:
        self._view(role)
        return [self.enrich_batch(batch) for batch in self.repository.list_batches()]

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_batch(self.repository.get_batch(batch_id))

    def correct_batch(self, batch_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch(batch_id)
        previous = {"approved_total": batch["approved_total"],
                    "allowed_areas": batch["allowed_areas"],
                    "valid_from": batch["valid_from"],
                    "valid_until": batch["valid_until"]}
        approved_total = (require_number(payload["approved_total"], "approved_total", 0.000001)
                          if "approved_total" in payload and payload["approved_total"] is not None
                          else None)
        allowed_areas = (require_area_list(payload["allowed_areas"])
                         if "allowed_areas" in payload and payload["allowed_areas"] is not None
                         else None)
        valid_from: Optional[str]
        if "valid_from" in payload and payload["valid_from"] is not None:
            valid_from = normalize_iso(parse_datetime(payload["valid_from"]))
        else:
            valid_from = None
        valid_until = (normalize_iso(require_datetime(payload["valid_until"], "valid_until"))
                       if "valid_until" in payload and payload["valid_until"] is not None
                       else None)
        expected = payload.get("expected_version")
        if expected is not None:
            if not isinstance(expected, int) or expected < 1:
                raise ValueError("expected_version必须是正整数")
        updated = self.repository.update_batch(batch_id, approved_total, allowed_areas,
                                               valid_from, valid_until, expected, actor)
        recalc = self._recalculate(updated)
        self.repository.append_audit("batch_correct", BATCH_ENTITY, batch_id, actor, {
            "batch_no": updated["batch_no"], "previous": previous,
            "approved_total": updated["approved_total"],
            "allowed_areas": updated["allowed_areas"],
            "valid_from": updated["valid_from"], "valid_until": updated["valid_until"],
            "remaining": recalc["remaining"],
            "conflicting_usages": [u["spray_id"] for u in recalc["conflicts"]],
        })
        result = self.enrich_batch(updated)
        result["recalculation"] = recalc
        return result

    def _recalculate(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        batch_dt = self._batch_with_dt(batch)
        used_total = self.repository.used_amount(batch["id"])
        open_sprays = self.repository.active_sprays(batch["id"], open_only=True)
        # 已关闭事件冻结：其消耗先占用新批准总量，剩余池子再按时间顺序分配给未结束事件
        open_active_total = sum(s["amount"] for s in open_sprays)
        pool = batch["approved_total"] - (used_total - open_active_total)
        remaining = round(batch["approved_total"] - used_total, 6)
        rows = []
        for spray in open_sprays:
            spray_conflicts = evaluate_spray(
                batch_dt, spray["amount"], parse_datetime(spray["started_at"]),
                parse_datetime(spray["ended_at"]), spray["area"],
                spray["latitude"], spray["longitude"], used_total)
            # 时点/海域冲突逐单判断；余量判断在汇总后按新值核算
            rows.append({"spray": spray,
                         "conflicts": [c for c in spray_conflicts if c["code"] != "over_quota"]})
        for row in rows:
            if row["conflicts"]:
                continue
            amount = row["spray"]["amount"]
            if amount > pool + 1e-9:
                row["conflicts"].append({
                    "code": "over_quota",
                    "message": f"批次更正后超量，剩余余量{remaining}",
                    "remaining": remaining, "approved_total": batch["approved_total"],
                })
            pool -= amount
        conflicts = [{
            "spray_id": row["spray"]["id"], "item_id": row["spray"]["item_id"],
            "amount": row["spray"]["amount"],
            "codes": [c["code"] for c in row["conflicts"]],
            "messages": [c["message"] for c in row["conflicts"]],
        } for row in rows if row["conflicts"]]
        return {
            "approved_total": batch["approved_total"], "used_total": round(used_total, 6),
            "remaining": remaining, "conflicts": conflicts,
        }

    def enrich_batch(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(batch)
        used_total = self.repository.used_amount(batch["id"])
        result["used_total"] = round(used_total, 6)
        result["remaining"] = round(batch["approved_total"] - used_total, 6)
        return result

    # ---- 消油剂喷洒登记 / 撤回 / 查询 ----
    def register_spray(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, SPRAY_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能登记喷洒")
        batch_id = require_number(payload.get("batch_id"), "batch_id", 1)
        if float(int(batch_id)) != batch_id:
            raise ValueError("batch_id必须是正整数")
        batch_id = int(batch_id)
        batch = self._batch_with_dt(self.repository.get_batch(batch_id))
        amount = require_number(payload.get("amount"), "amount", 0.000001)
        started_dt = require_datetime(payload.get("started_at"), "started_at")
        ended_dt = require_datetime(payload.get("ended_at"), "ended_at")
        if started_dt > ended_dt:
            raise ValidationError("结束时间不能早于开始时间")
        area = require_text(payload.get("area"), "area", 100)
        latitude = require_number(payload.get("latitude"), "latitude", -90, 90)
        longitude = require_number(payload.get("longitude"), "longitude", -180, 180)
        vessel = require_text(payload.get("vessel"), "vessel", 200)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        # 校验在写入前完成，任一项冲突则整单不保存
        used_total = self.repository.used_amount(batch_id)
        conflicts = evaluate_spray(batch, amount, started_dt, ended_dt, area,
                                   latitude, longitude, used_total)
        if conflicts:
            raise ConflictError("喷洒登记与批次冲突，整单未保存", {
                "batch_id": batch_id, "batch_no": batch["batch_no"],
                "remaining": round(batch["approved_total"] - used_total, 6),
                "conflicts": conflicts,
            })
        spray = self.repository.add_spray(
            item_id, batch_id, amount, normalize_iso(started_dt), normalize_iso(ended_dt),
            area, latitude, longitude, vessel, external_ref, actor)
        new_remaining = round(batch["approved_total"] - used_total - amount, 6)
        self.repository.append_audit("spray_register", SPRAY_ENTITY, spray["id"], actor, {
            "spray_id": spray["id"], "item_id": item_id, "batch_id": batch_id,
            "batch_no": batch["batch_no"], "amount": amount, "unit": batch["unit"],
            "started_at": spray["started_at"], "ended_at": spray["ended_at"],
            "area": area, "latitude": latitude, "longitude": longitude,
            "vessel": vessel, "remaining": new_remaining,
        })
        result = dict(spray)
        result["batch_no"] = batch["batch_no"]
        result["remaining"] = new_remaining
        return result

    def withdraw_spray(self, spray_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, SPRAY_ROLES)
        actor = require_text(actor, "actor", 100)
        spray = self.repository.get_spray(spray_id)
        item = self.repository.get_item(spray["item_id"])
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能撤回喷洒")
        reason = require_text(payload.get("reason"), "reason")
        updated = self.repository.withdraw_spray(spray_id, reason, actor)
        batch = self.repository.get_batch(updated["batch_id"])
        remaining = self.enrich_batch(batch)["remaining"]
        self.repository.append_audit("spray_withdraw", SPRAY_ENTITY, spray_id, actor, {
            "spray_id": spray_id, "item_id": updated["item_id"],
            "batch_id": updated["batch_id"], "batch_no": batch["batch_no"],
            "amount": updated["amount"], "reason": reason,
            "released": True, "remaining": remaining,
        })
        result = dict(updated)
        result["batch_no"] = batch["batch_no"]
        result["remaining"] = remaining
        return result

    def list_sprays(self, role: str, item_id: Optional[int] = None,
                    batch_id: Optional[int] = None) -> list:
        self._view(role)
        sprays = self.repository.list_sprays(item_id, batch_id)
        batch_cache: Dict[int, Dict[str, Any]] = {}
        result: List[Dict[str, Any]] = []
        for spray in sprays:
            row = dict(spray)
            bid = spray["batch_id"]
            if bid not in batch_cache:
                batch_cache[bid] = self.repository.get_batch(bid)
            row["batch_no"] = batch_cache[bid]["batch_no"]
            result.append(row)
        return result

    def batch_usage(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.enrich_batch(self.repository.get_batch(batch_id))
        sprays = self.repository.list_sprays(batch_id=batch_id)
        active = [s for s in sprays if s["status"] == "active"]
        withdrawn = [s for s in sprays if s["status"] == "withdrawn"]
        batch["active_count"] = len(active)
        batch["withdrawn_count"] = len(withdrawn)
        batch["usages"] = sprays
        return batch

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
