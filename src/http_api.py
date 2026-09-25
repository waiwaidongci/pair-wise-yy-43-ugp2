from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            payload: Dict[str, Any] = {"error": exc.__class__.__name__, "message": str(exc)}
            details = getattr(exc, "details", None)
            if details:
                payload["details"] = details
            self._json(status, payload)

        @staticmethod
        def _int_id(value: str) -> int:
            try:
                result = int(value)
            except (TypeError, ValueError) as exc:
                raise NotFoundError("资源不存在") from exc
            if result < 1:
                raise NotFoundError("资源不存在")
            return result

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/batches":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"batches": service.list_batches(role)})
                elif path.startswith("/api/batches/") and path.endswith("/usage"):
                    batch_id = self._int_id(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.batch_usage(batch_id, role))
                elif path.startswith("/api/batches/"):
                    batch_id = self._int_id(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_batch(batch_id, role))
                elif path == "/api/sprays":
                    actor, role = self._identity()
                    del actor
                    item_id = self._opt_int(query.get("item_id", [None])[0])
                    batch_id = self._opt_int(query.get("batch_id", [None])[0])
                    self._json(200, {"usages": service.list_sprays(role, item_id, batch_id)})
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = self._int_id(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/") and path.endswith("/sprays"):
                    item_id = self._int_id(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"usages": service.list_sprays(role, item_id=item_id)})
                elif path.startswith("/api/items/"):
                    item_id = self._int_id(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        @staticmethod
        def _opt_int(value: Optional[str]) -> Optional[int]:
            if value is None or value == "":
                return None
            try:
                result = int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError("查询参数必须是整数") from exc
            return result if result >= 1 else None

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path == "/api/batches":
                    self._json(201, service.create_batch(body, actor, role))
                elif path.startswith("/api/batches/") and path.endswith("/correct"):
                    batch_id = self._int_id(path.split("/")[3])
                    self._json(200, service.correct_batch(batch_id, body, actor, role))
                elif path.startswith("/api/sprays/") and path.endswith("/withdraw"):
                    spray_id = self._int_id(path.split("/")[3])
                    self._json(200, service.withdraw_spray(spray_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = self._int_id(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/sprays"):
                    item_id = self._int_id(path.split("/")[3])
                    self._json(201, service.register_spray(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = self._int_id(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
