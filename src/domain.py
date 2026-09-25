from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message,details:Optional[Dict[str,Any]]=None):
        super().__init__(message); self.message=message; self.details=details
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; ROLES=['observer', 'response_commander', 'operations', 'viewer']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class Batch:
    id:int; batch_no:str; approved_total:float; unit:str; allowed_areas:List[str]; valid_from:Optional[str]; valid_until:str; version:int; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class SprayUsage:
    id:int; item_id:int; batch_id:int; amount:float; started_at:str; ended_at:str; area:str; latitude:float; longitude:float; vessel:str; status:str; withdraw_reason:Optional[str]; withdrawn_by:Optional[str]; withdrawn_at:Optional[str]; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0,maximum=None):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    if maximum is not None and number>maximum: raise ValidationError(f"{field}不能大于{maximum}")
    return number
def parse_datetime(value) -> datetime:
    if isinstance(value,datetime): dt=value
    else:
        if not isinstance(value,str) or not value.strip(): raise ValidationError("时间必须是ISO8601字符串")
        try: dt=datetime.fromisoformat(value.strip().replace("Z","+00:00"))
        except ValueError as exc: raise ValidationError("时间必须是ISO8601字符串") from exc
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
def require_datetime(value,field) -> datetime:
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    return parse_datetime(value)
def normalize_iso(dt:datetime) -> str:
    return dt.replace(microsecond=0).isoformat()
def require_area_list(value,field="allowed_areas"):
    if not isinstance(value,list) or not value: raise ValidationError(f"{field}必须是非空数组")
    areas=[]
    for raw in value:
        area=require_text(raw,field,100)
        if area not in areas: areas.append(area)
    return areas
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
