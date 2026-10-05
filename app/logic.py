"""核心业务逻辑：参数校验、线性漂移映射、声段投影排序、版本差异计算。

时间约定：所有时间均为整数毫秒。
- local_ms：设备本地时钟毫秒（如设备开机以来的毫秒数）；
- utc_ms ：Unix 纪元毫秒（UTC）。

对齐算法：每台设备在相邻锚点间按线性漂移把本地时间映射为 UTC：
    utc(t) = u0 + (t - l0) * (u1 - u0) / (l1 - l0)
其中 (l0, u0)、(l1, u1) 为夹住 t 的相邻锚点。结果四舍五入（半数向上）到毫秒整数。
t 恰好等于某锚点本地时间时取该锚点 UTC。落在锚点覆盖范围 [首锚点, 末锚点]
之外的时刻无法映射。
"""
from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left

MAX_INT64 = 2 ** 63 - 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

STATUS_LOCATED = "located"
STATUS_UNLOCATABLE = "unlocatable"

# 默认周期：未显式指定 cycle_id 的锚点/声段一律归入此周期。
DEFAULT_CYCLE_ID = "default"


class ApiError(Exception):
    """业务错误，直接映射为 HTTP 错误响应。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


# ---------------------------------------------------------------- 参数校验

def require_payload(body) -> dict:
    if body is None:
        raise ApiError(400, "validation_error", "请求体不能为空，必须是 JSON 对象")
    if not isinstance(body, dict):
        raise ApiError(400, "validation_error", "请求体必须是 JSON 对象",
                       {"received_type": type(body).__name__})
    return body


def require_fields(payload: dict, fields: list[str]) -> None:
    missing = [f for f in fields if f not in payload]
    if missing:
        raise ApiError(400, "validation_error",
                       "缺少必填字段: " + ", ".join(missing), {"missing": missing})


def validate_ms(value, field: str) -> int:
    """毫秒时间必须是 [0, 2^63-1] 内的 JSON 整数（布尔、浮点、字符串一律拒绝）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_time",
                       f"时间字段 {field} 必须是毫秒整数（int64）",
                       {"field": field, "value": value})
    if value < 0 or value > MAX_INT64:
        raise ApiError(400, "invalid_time",
                       f"时间字段 {field} 超出有效范围 [0, {MAX_INT64}]",
                       {"field": field, "value": value})
    return value


def validate_id(value, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ApiError(400, "validation_error",
                       f"{field} 必须以字母或数字开头，仅含字母、数字、_ . - ，长度 1-128",
                       {"field": field, "value": value})
    return value


def validate_cycle_id(value) -> str:
    """周期编号：省略/缺省 -> 默认周期；否则按与设备/采集编号相同的命名规则校验。"""
    if value is None:
        return DEFAULT_CYCLE_ID
    return validate_id(value, "cycle_id")


def validate_note(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise ApiError(400, "validation_error", "note 必须是不超过 500 字符的字符串",
                       {"value": value})
    return value


def validate_fingerprint(value) -> str:
    """封存请求携带的周期内容指纹：64 位小写十六进制（sha256）。"""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ApiError(400, "validation_error",
                       "fingerprint 必须是 64 位小写十六进制字符串（sha256），"
                       "请从 GET /devices/{id}/cycles/{cycle_id} 周期详情中获取",
                       {"value": value})
    return value


# ---------------------------------------------------------------- 漂移映射

def _interpolate(l0: int, u0: int, l1: int, u1: int, t: int) -> int:
    """整数线性插值，四舍五入（半数向上）。分子分母恒非负。"""
    num = (t - l0) * (u1 - u0)
    den = l1 - l0
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return u0 + q


def map_local_to_utc(locals_list: list[int], utcs_list: list[int], t: int) -> int | None:
    """把设备本地毫秒映射为 UTC 毫秒；超出锚点覆盖范围返回 None。"""
    n = len(locals_list)
    if n == 0:
        return None
    i = bisect_left(locals_list, t)
    if i < n and locals_list[i] == t:
        return utcs_list[i]
    if i == 0 or i == n:
        return None
    return _interpolate(locals_list[i - 1], utcs_list[i - 1],
                        locals_list[i], utcs_list[i], t)


# ---------------------------------------------------------------- 声段投影

def project_segment(seg: dict, anchors_pair) -> dict:
    """根据设备当前锚点计算声段的 UTC 投影。

    起止任一时刻落在锚点覆盖范围之外 -> status = unlocatable，UTC 字段为 null。
    """
    locals_list, utcs_list = anchors_pair
    utc_start = map_local_to_utc(locals_list, utcs_list, seg["local_start_ms"])
    utc_end = map_local_to_utc(locals_list, utcs_list, seg["local_end_ms"])
    located = utc_start is not None and utc_end is not None
    out = dict(seg)
    out["status"] = STATUS_LOCATED if located else STATUS_UNLOCATABLE
    out["utc_start_ms"] = utc_start if located else None
    out["utc_end_ms"] = utc_end if located else None
    return out


def sort_projections(projs: list[dict]) -> list[dict]:
    """稳定排序：可定位声段按 (UTC 起点, 设备编号, 采集编号) 升序；
    无法定位的声段排在最后，按 (设备编号, 采集编号) 升序。"""

    def key(p: dict):
        if p["status"] == STATUS_LOCATED:
            return (0, p["utc_start_ms"], p["device_id"], p["capture_id"])
        return (1, 0, p["device_id"], p["capture_id"])

    return sorted(projs, key=key)


def segment_content_hash(capture_id: str, device_id: str, cycle_id: str,
                         local_start_ms: int, local_end_ms: int,
                         note: str | None) -> str:
    """声段内容指纹：用于"同编号同内容幂等、异内容冲突"的判定。

    周期计入内容：同采集编号仅当（设备、周期、本地起止、note）全一致才算同内容。
    """
    canonical = json.dumps(
        {"capture_id": capture_id, "device_id": device_id, "cycle_id": cycle_id,
         "local_start_ms": local_start_ms, "local_end_ms": local_end_ms,
         "note": note},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 周期封存

def cycle_fingerprint(device_id: str, cycle_id: str,
                      anchors: list[dict], segments: list[dict]) -> str:
    """周期内容稳定指纹：由该周期全部锚点与声段内容生成（sha256）。

    - 锚点须已按 local_ms 升序、声段须已按 capture_id 升序（调用方保证），
      因此同样的内容无论读取多少次都得到同一个指纹；
    - 声段只计入录入内容（编号、本地起止、note）；UTC 投影由锚点推导，
      锚点已在指纹中，不重复计入；
    - 锚点或声段任何一处增删改都会改变指纹，封存请求据此核对"当前内容"。
    """
    canonical = json.dumps(
        {"device_id": device_id, "cycle_id": cycle_id,
         "anchors": [{"local_ms": a["local_ms"], "utc_ms": a["utc_ms"]}
                     for a in anchors],
         "segments": [{"capture_id": s["capture_id"],
                       "local_start_ms": s["local_start_ms"],
                       "local_end_ms": s["local_end_ms"],
                       "note": s["note"]} for s in segments]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 版本差异

def compute_changes(prev_list: list[dict], curr_list: list[dict]) -> dict:
    """比较相邻两版声段投影，给出新增、移除与位移。

    位移：两版都存在的声段，其定位状态或 UTC 起止发生变化。
    start_shift_ms / end_shift_ms 仅在两版均可定位时给出（毫秒差值），否则为 null。
    """
    prev = {p["capture_id"]: p for p in prev_list}
    curr = {p["capture_id"]: p for p in curr_list}

    added = sorted((curr[c] for c in curr.keys() - prev.keys()),
                   key=lambda p: (p["device_id"], p["capture_id"]))
    removed = sorted((prev[c] for c in prev.keys() - curr.keys()),
                     key=lambda p: (p["device_id"], p["capture_id"]))

    displaced = []
    for c in sorted(curr.keys() & prev.keys()):
        p, n = prev[c], curr[c]
        if (p["status"], p["utc_start_ms"], p["utc_end_ms"]) != \
           (n["status"], n["utc_start_ms"], n["utc_end_ms"]):
            both = p["status"] == STATUS_LOCATED and n["status"] == STATUS_LOCATED
            displaced.append({
                "capture_id": c,
                "device_id": n["device_id"],
                "previous": {"status": p["status"],
                             "utc_start_ms": p["utc_start_ms"],
                             "utc_end_ms": p["utc_end_ms"]},
                "current": {"status": n["status"],
                            "utc_start_ms": n["utc_start_ms"],
                            "utc_end_ms": n["utc_end_ms"]},
                "start_shift_ms": (n["utc_start_ms"] - p["utc_start_ms"]) if both else None,
                "end_shift_ms": (n["utc_end_ms"] - p["utc_end_ms"]) if both else None,
            })
    displaced.sort(key=lambda d: (d["device_id"], d["capture_id"]))
    return {"added": added, "removed": removed, "displaced": displaced}
