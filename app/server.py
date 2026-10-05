"""HTTP 层：路由、JSON 编解码、统一错误响应。仅使用标准库 http.server。"""
from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, service
from .logic import ApiError, DEFAULT_CYCLE_ID, validate_id

MAX_BODY_BYTES = 10 * 1024 * 1024


class Context:
    """请求处理上下文（db_path 可被测试替换）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path


def _parse_int_param(value: str, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "validation_error",
                       f"路径参数 {name} 必须是整数", {"value": value})


def _cycle_query(query) -> str | None:
    """解析 ?cycle_id= 过滤参数；未提供时返回 None（表示不限定/按默认处理）。"""
    value = query.get("cycle_id", [None])[0]
    if value is None:
        return None
    return validate_id(value, "cycle_id")


# ---------------------------------------------------------------- 路由处理

def h_root(ctx, body, query):
    return 200, {
        "service": "timeline-align",
        "version": __version__,
        "description": "多设备现场录音时间轴对齐服务",
        "endpoints": [
            "GET    /health",
            "POST   /devices",
            "GET    /devices",
            "GET    /devices/{device_id}",
            "POST   /devices/{device_id}/cycles                    # 登记时钟周期（重启轮次）",
            "GET    /devices/{device_id}/cycles",
            "GET    /devices/{device_id}/cycles/{cycle_id}         # 周期详情（内容指纹+封存状态）",
            "POST   /devices/{device_id}/cycles/{cycle_id}/seal    # 封存周期（携带指纹，不可撤销）",
            "POST   /devices/{device_id}/anchors                   # body 可带 cycle_id，省略=default",
            "GET    /devices/{device_id}/anchors                   # 可加 ?cycle_id= 过滤",
            "PUT    /devices/{device_id}/anchors            # 锚点更正（按周期整体替换）",
            "DELETE /devices/{device_id}/anchors/{local_ms} # 锚点更正（删除单个，默认 default 周期）",
            "POST   /segments                                      # body 可带 cycle_id，省略=default",
            "POST   /segments/reassign-cycle           # 批量纠正声段周期归属（整批原子）",
            "GET    /segments                                      # 可加 ?device_id=&cycle_id=",
            "GET    /segments/{capture_id}",
            "DELETE /segments/{capture_id}",
            "GET    /timeline/current",
            "POST   /coverage/audit                # 覆盖审计 {start_ms,end_ms,min_devices,version_no?}（只读）",
            "POST   /versions",
            "GET    /versions",
            "GET    /versions/{version_no}",
        ],
    }


def h_health(ctx, body, query):
    return 200, {"status": "ok", "time": service.iso_now()}


def h_create_device(ctx, body, query):
    return service.create_device(ctx.db_path, body)


def h_list_devices(ctx, body, query):
    return service.list_devices(ctx.db_path)


def h_get_device(ctx, body, query, device_id):
    return service.get_device(ctx.db_path, device_id)


def h_register_cycle(ctx, body, query, device_id):
    return service.register_cycle(ctx.db_path, device_id, body)


def h_list_cycles(ctx, body, query, device_id):
    return service.list_cycles(ctx.db_path, device_id)


def h_get_cycle(ctx, body, query, device_id, cycle_id):
    return service.get_cycle_detail(ctx.db_path, device_id,
                                    validate_id(cycle_id, "cycle_id"))


def h_seal_cycle(ctx, body, query, device_id, cycle_id):
    return service.seal_cycle(ctx.db_path, device_id,
                              validate_id(cycle_id, "cycle_id"), body)


def h_add_anchor(ctx, body, query, device_id):
    return service.add_anchor(ctx.db_path, device_id, body)


def h_list_anchors(ctx, body, query, device_id):
    return service.list_anchors(ctx.db_path, device_id, _cycle_query(query))


def h_replace_anchors(ctx, body, query, device_id):
    return service.replace_anchors(ctx.db_path, device_id, body)


def h_delete_anchor(ctx, body, query, device_id, local_ms):
    ms = _parse_int_param(local_ms, "local_ms")
    if ms < 0:
        raise ApiError(400, "invalid_time", "local_ms 不能为负", {"local_ms": ms})
    cycle_id = _cycle_query(query) or DEFAULT_CYCLE_ID
    return service.delete_anchor(ctx.db_path, device_id, ms, cycle_id)


def h_submit_segment(ctx, body, query):
    return service.submit_segment(ctx.db_path, body)


def h_reassign_segment_cycles(ctx, body, query):
    return service.reassign_segment_cycles(ctx.db_path, body)


def h_list_segments(ctx, body, query):
    device_id = query.get("device_id", [None])[0]
    cycle_id = _cycle_query(query)
    return service.list_segments(ctx.db_path, device_id, cycle_id)


def h_get_segment(ctx, body, query, capture_id):
    return service.get_segment(ctx.db_path, capture_id)


def h_delete_segment(ctx, body, query, capture_id):
    return service.delete_segment(ctx.db_path, capture_id)


def h_current_timeline(ctx, body, query):
    return service.current_timeline(ctx.db_path)


def h_coverage_audit(ctx, body, query):
    return service.coverage_audit(ctx.db_path, body)


def h_publish(ctx, body, query):
    return service.publish_version(ctx.db_path, body)


def h_list_versions(ctx, body, query):
    return service.list_versions(ctx.db_path)


def h_get_version(ctx, body, query, version_no):
    no = _parse_int_param(version_no, "version_no")
    if no < 1:
        raise ApiError(404, "version_not_found", f"版本 {no} 不存在", {"version_no": no})
    return service.get_version(ctx.db_path, no)


ROUTES = [
    ("GET", r"^/$", h_root),
    ("GET", r"^/health$", h_health),
    ("POST", r"^/devices$", h_create_device),
    ("GET", r"^/devices$", h_list_devices),
    ("GET", r"^/devices/(?P<device_id>[^/]+)$", h_get_device),
    ("POST", r"^/devices/(?P<device_id>[^/]+)/cycles$", h_register_cycle),
    ("GET", r"^/devices/(?P<device_id>[^/]+)/cycles$", h_list_cycles),
    ("GET", r"^/devices/(?P<device_id>[^/]+)/cycles/(?P<cycle_id>[^/]+)$", h_get_cycle),
    ("POST", r"^/devices/(?P<device_id>[^/]+)/cycles/(?P<cycle_id>[^/]+)/seal$", h_seal_cycle),
    ("POST", r"^/devices/(?P<device_id>[^/]+)/anchors$", h_add_anchor),
    ("GET", r"^/devices/(?P<device_id>[^/]+)/anchors$", h_list_anchors),
    ("PUT", r"^/devices/(?P<device_id>[^/]+)/anchors$", h_replace_anchors),
    ("DELETE", r"^/devices/(?P<device_id>[^/]+)/anchors/(?P<local_ms>[^/]+)$", h_delete_anchor),
    ("POST", r"^/segments$", h_submit_segment),
    ("POST", r"^/segments/reassign-cycle$", h_reassign_segment_cycles),
    ("GET", r"^/segments$", h_list_segments),
    ("GET", r"^/segments/(?P<capture_id>[^/]+)$", h_get_segment),
    ("DELETE", r"^/segments/(?P<capture_id>[^/]+)$", h_delete_segment),
    ("GET", r"^/timeline/current$", h_current_timeline),
    ("POST", r"^/coverage/audit$", h_coverage_audit),
    ("POST", r"^/versions$", h_publish),
    ("GET", r"^/versions$", h_list_versions),
    ("GET", r"^/versions/(?P<version_no>[^/]+)$", h_get_version),
]
_COMPILED = [(m, re.compile(p), f) for m, p, f in ROUTES]


# ---------------------------------------------------------------- 请求分发

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"TimelineAlign/{__version__}"

    # 保持日志简洁，走 stderr
    def log_message(self, fmt, *args):
        import sys
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # ------------------------------------------------------------ 内部

    def _dispatch(self, method: str):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/")
            query = parse_qs(parsed.query)

            path_matched = False
            for m, pattern, func in _COMPILED:
                match = pattern.match(path)
                if not match:
                    continue
                if m != method:
                    path_matched = True
                    continue
                body = self._read_body() if method in ("POST", "PUT") else None
                if method in ("GET", "DELETE"):
                    self._drain_body()
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                status, obj = func(self.server.ctx, body, query, **params)
                self._send_json(status, obj)
                return
            if path_matched:
                self._send_json(405, {"error": {"code": "method_not_allowed",
                                                "message": f"{method} {path} 方法不允许",
                                                "details": {}}})
            else:
                self._send_json(404, {"error": {"code": "not_found",
                                                "message": f"路径不存在: {method} {path}",
                                                "details": {}}})
        except ApiError as e:
            self._send_json(e.status, e.body())
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "服务器内部错误", "details": {}}})

    def _content_length(self) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            return max(0, int(raw))
        except ValueError:
            raise ApiError(400, "validation_error", "Content-Length 非法", {"value": raw})

    def _drain_body(self):
        n = self._content_length()
        if n:
            self.rfile.read(n)

    def _read_body(self):
        n = self._content_length()
        if n == 0:
            return None
        if n > MAX_BODY_BYTES:
            raise ApiError(413, "payload_too_large",
                           f"请求体超过 {MAX_BODY_BYTES} 字节上限")
        ctype = self.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            raise ApiError(415, "unsupported_media_type",
                           "Content-Type 必须是 application/json")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError(400, "invalid_json", f"请求体不是合法 JSON: {e}")

    def _send_json(self, status: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.ctx = Context(db_path)
    return httpd
