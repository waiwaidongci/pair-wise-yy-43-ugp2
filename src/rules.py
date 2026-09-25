from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'; BATCH_ENTITY='消油剂批次'; SPRAY_ENTITY='消油剂喷洒'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
BATCH_CREATE_ROLES=set(['response_commander']); BATCH_CORRECT_ROLES=set(['response_commander']); SPRAY_ROLES=set(['response_commander', 'operations'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def validate_window(started: datetime, ended: datetime, valid_from: Optional[datetime], valid_until: datetime) -> List[Dict[str,Any]]:
    conflicts=[]
    if valid_from is not None and started<valid_from:
        conflicts.append({"code":"batch_not_effective","message":f"批次{valid_from.isoformat()}之后才生效"})
    if ended>valid_until:
        conflicts.append({"code":"batch_expired","message":f"批次已于{valid_until.isoformat()}过期"})
    return conflicts
def evaluate_spray(batch: Dict[str,Any], amount: float, started: datetime, ended: datetime,
                   area: str, latitude: float, longitude: float, used_total: float):
    del latitude, longitude
    conflicts=validate_window(started, ended, batch["valid_from_dt"], batch["valid_until_dt"])
    if area not in batch["allowed_areas"]:
        conflicts.append({"code":"area_mismatch","message":f"海域{area}不在批次适用范围：{','.join(batch['allowed_areas'])}"})
    remaining=batch["approved_total"]-used_total
    if amount>remaining+1e-9:
        conflicts.append({"code":"over_quota","message":f"超出批次批准总量，剩余余量{round(remaining,3)}","remaining":round(remaining,3),"approved_total":batch["approved_total"]})
    return conflicts
