"""服务层：组合存储与业务逻辑，每个公开函数对应一类 API 操作。

返回 (HTTP 状态码, 响应体 dict)。所有写路径都在单个 BEGIN IMMEDIATE
事务内完成"校验 + 写入"，失败即回滚，不产生半写入状态。

周期（cycle）：设备每次重启本地毫秒归零，因此锚点与声段都归属于某个
"设备内唯一"的时钟周期。省略 cycle_id 时一律归入 default 默认周期；
锚点递增校验与线性漂移映射均按 (设备, 周期) 独立执行；声段投影只使用
其所属周期的锚点。

周期封存（seal）：现场核对完成后可封存单个周期——封存请求携带周期详情
中的内容指纹，在同一事务内核对当前内容且全部声段可定位才生效；封存后
该周期的锚点/声段变更一律拒绝（同内容声段重传仍按幂等规则返回），
封存不可撤销。

周期归属批量纠正（reassign，v1.3）：现场整理时发现一批声段被记到错误的
时钟周期，可按采集编号批量把它们在同一设备的已登记周期之间移动；每项
声明预期原周期与目标周期，任一项无效即整批拒绝（不改动任何数据）；
成功后声段起止/备注/创建时间/采集编号不变，内容指纹按新周期重算，
各声段按目标周期锚点重算定位，相关周期内容指纹随之更新。
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db
from .logic import (ApiError, DEFAULT_CYCLE_ID, STATUS_UNLOCATABLE,
                    compute_changes, cycle_fingerprint, project_segment,
                    require_fields, require_payload, segment_content_hash,
                    sort_projections, validate_cycle_id, validate_fingerprint,
                    validate_id, validate_ms, validate_note)
from .db import SCHEMA_VERSION


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- 内部工具

def _get_device(conn, device_id: str) -> dict:
    row = conn.execute(
        "SELECT device_id, name, created_at FROM devices WHERE device_id=?",
        (device_id,)).fetchone()
    if not row:
        raise ApiError(404, "device_not_found",
                       f"设备 {device_id} 不存在，请先通过 POST /devices 注册",
                       {"device_id": device_id})
    return dict(row)


def _get_cycle(conn, device_id: str, cycle_id: str) -> dict:
    row = conn.execute(
        "SELECT device_id, cycle_id, note, sealed_at, seal_fingerprint, created_at "
        "FROM cycles WHERE device_id=? AND cycle_id=?",
        (device_id, cycle_id)).fetchone()
    if not row:
        raise ApiError(404, "cycle_not_found",
                       f"设备 {device_id} 不存在周期 {cycle_id}，"
                       f"请先通过 POST /devices/{device_id}/cycles 登记",
                       {"device_id": device_id, "cycle_id": cycle_id})
    return dict(row)


def _raise_if_sealed(cycle: dict) -> None:
    """周期已封存 -> 409：该周期锚点与声段已冻结，拒绝一切变更（封存不可撤销）。"""
    if cycle.get("sealed_at") is not None:
        raise ApiError(
            409, "cycle_sealed",
            f"设备 {cycle['device_id']} 的周期 {cycle['cycle_id']} "
            f"已于 {cycle['sealed_at']} 封存，该周期的锚点与声段已冻结，拒绝变更",
            {"device_id": cycle["device_id"], "cycle_id": cycle["cycle_id"],
             "sealed_at": cycle["sealed_at"]})


def _cycles_of(conn, device_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT device_id, cycle_id, note, sealed_at, created_at FROM cycles "
        "WHERE device_id=? ORDER BY cycle_id", (device_id,)).fetchall()
    cycles = []
    for r in rows:
        d = dict(r)
        d["sealed"] = d["sealed_at"] is not None
        cycles.append(d)
    return cycles


def _anchors_of(conn, device_id: str, cycle_id: str | None = None) -> list[dict]:
    """读取设备锚点（含周期归属），按周期、本地时间升序。

    cycle_id 非空时只返回该周期的锚点。
    """
    if cycle_id is None:
        rows = conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms FROM anchors "
            "WHERE device_id=? ORDER BY cycle_id, local_ms",
            (device_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms FROM anchors "
            "WHERE device_id=? AND cycle_id=? ORDER BY local_ms",
            (device_id, cycle_id)).fetchall()
    return [dict(r) for r in rows]


def _anchors_map(conn) -> dict:
    """(device_id, cycle_id) -> (locals_list, utcs_list)，按本地时间升序。"""
    m: dict[tuple[str, str], tuple[list[int], list[int]]] = {}
    for r in conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms "
            "FROM anchors ORDER BY device_id, cycle_id, local_ms"):
        pair = m.setdefault((r["device_id"], r["cycle_id"]), ([], []))
        pair[0].append(r["local_ms"])
        pair[1].append(r["utc_ms"])
    return m


def _cycle_contents(conn, device_id: str, cycle_id: str):
    """读取周期当前内容：锚点（按 local_ms 升序）与声段（按 capture_id 升序）。

    排序固定，因此同一内容算出的周期指纹稳定。
    """
    anchors = _anchors_of(conn, device_id, cycle_id)
    seg_rows = conn.execute(
        "SELECT * FROM segments WHERE device_id=? AND cycle_id=? ORDER BY capture_id",
        (device_id, cycle_id)).fetchall()
    return anchors, [_row_to_segment(r) for r in seg_rows]


def _cycle_content_fingerprint(conn, device_id: str, cycle_id: str) -> str:
    anchors, segs = _cycle_contents(conn, device_id, cycle_id)
    return cycle_fingerprint(device_id, cycle_id, anchors, segs)


def _row_to_segment(row) -> dict:
    return {"capture_id": row["capture_id"], "device_id": row["device_id"],
            "cycle_id": row["cycle_id"],
            "local_start_ms": row["local_start_ms"],
            "local_end_ms": row["local_end_ms"],
            "note": row["note"], "created_at": row["created_at"]}


def _project_all(conn, seg_rows) -> list[dict]:
    anchors = _anchors_map(conn)
    projs = [project_segment(_row_to_segment(r),
                             anchors.get((r["device_id"], r["cycle_id"]), ([], [])))
             for r in seg_rows]
    return sort_projections(projs)


def _version_row_to_proj(r) -> dict:
    return {"capture_id": r["capture_id"], "device_id": r["device_id"],
            "cycle_id": r["cycle_id"],
            "local_start_ms": r["local_start_ms"],
            "local_end_ms": r["local_end_ms"], "note": r["note"],
            "created_at": r["created_at"],
            "status": r["status"],
            "utc_start_ms": r["utc_start_ms"], "utc_end_ms": r["utc_end_ms"]}


def _strip_cycle_fields(obj):
    """旧格式（format_rev=1）快照：递归剔除周期字段，保证旧版本查询结果不变。"""
    if isinstance(obj, dict):
        return {k: _strip_cycle_fields(v) for k, v in obj.items() if k != "cycle_id"}
    if isinstance(obj, list):
        return [_strip_cycle_fields(v) for v in obj]
    return obj


def _check_insert_increasing(existing: list[dict], local_ms: int, utc_ms: int) -> None:
    """新锚点插入后，该周期全序列的本地时间与 UTC 时间都必须保持严格递增。"""
    prev = nxt = None
    for a in existing:
        if a["local_ms"] == local_ms:
            raise ApiError(
                400, "anchors_not_increasing",
                f"锚点本地时间 local_ms={local_ms} 在本周期已存在，"
                f"同一周期内锚点本地时间必须严格递增；"
                f"不同周期可复用本地毫秒，如需更正请用 PUT 整体替换锚点表，"
                f"或先 DELETE 该锚点再重录",
                {"local_ms": local_ms})
        if a["local_ms"] < local_ms:
            prev = a
        elif nxt is None:
            nxt = a
    if prev and prev["utc_ms"] >= utc_ms:
        raise ApiError(400, "anchors_not_increasing",
                       f"锚点 UTC 时间必须严格递增：新锚点 utc_ms={utc_ms} "
                       f"未大于前一锚点 (local_ms={prev['local_ms']}, utc_ms={prev['utc_ms']})",
                       {"prev": prev, "utc_ms": utc_ms})
    if nxt and nxt["utc_ms"] <= utc_ms:
        raise ApiError(400, "anchors_not_increasing",
                       f"锚点 UTC 时间必须严格递增：新锚点 utc_ms={utc_ms} "
                       f"未小于后一锚点 (local_ms={nxt['local_ms']}, utc_ms={nxt['utc_ms']})",
                       {"next": nxt, "utc_ms": utc_ms})


# ---------------------------------------------------------------- 设备

def create_device(db_path: str, body):
    payload = require_payload(body)
    require_fields(payload, ["device_id"])
    device_id = validate_id(payload["device_id"], "device_id")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise ApiError(400, "validation_error", "name 必须是字符串", {"value": name})
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            row = conn.execute(
                "SELECT device_id, name, created_at FROM devices WHERE device_id=?",
                (device_id,)).fetchone()
            if row:
                if row["name"] == name:
                    return 200, {"device": dict(row), "idempotent_replay": True}
                raise ApiError(409, "device_conflict",
                               f"设备 {device_id} 已存在且 name 不一致",
                               {"existing": dict(row), "received": {"device_id": device_id, "name": name}})
            now = iso_now()
            conn.execute("INSERT INTO devices(device_id, name, created_at) VALUES (?,?,?)",
                         (device_id, name, now))
            # 注册设备即隐式创建默认时钟周期（本地毫秒从 0 起算的本轮）
            conn.execute(
                "INSERT INTO cycles(device_id, cycle_id, note, created_at) VALUES (?,?,?,?)",
                (device_id, DEFAULT_CYCLE_ID, None, now))
            return 201, {"device": {"device_id": device_id, "name": name,
                                    "created_at": now},
                         "idempotent_replay": False}


def list_devices(db_path: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        rows = conn.execute(
            """SELECT d.device_id, d.name, d.created_at,
                      (SELECT COUNT(*) FROM anchors a WHERE a.device_id = d.device_id) AS anchor_count,
                      (SELECT COUNT(*) FROM segments s WHERE s.device_id = d.device_id) AS segment_count
               FROM devices d ORDER BY d.device_id""").fetchall()
        return 200, {"devices": [dict(r) for r in rows]}


def get_device(db_path: str, device_id: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        device = _get_device(conn, device_id)
        device["cycles"] = _cycles_of(conn, device_id)
        device["anchors"] = _anchors_of(conn, device_id)
        device["segment_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM segments WHERE device_id=?",
            (device_id,)).fetchone()["c"]
        return 200, {"device": device}


# ---------------------------------------------------------------- 周期

def register_cycle(db_path: str, device_id: str, body):
    """登记设备的一个时钟周期（设备重启后本地毫秒归零的新一轮）。"""
    payload = require_payload(body)
    require_fields(payload, ["cycle_id"])
    cycle_id = validate_id(payload["cycle_id"], "cycle_id")
    note = validate_note(payload.get("note"))
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            row = conn.execute(
                "SELECT device_id, cycle_id, note, created_at FROM cycles "
                "WHERE device_id=? AND cycle_id=?",
                (device_id, cycle_id)).fetchone()
            if row:
                if row["note"] == note:
                    return 200, {"cycle": dict(row), "idempotent_replay": True}
                raise ApiError(409, "cycle_conflict",
                               f"设备 {device_id} 的周期 {cycle_id} 已存在且 note 不一致",
                               {"existing": dict(row),
                                "received": {"cycle_id": cycle_id, "note": note}})
            now = iso_now()
            conn.execute(
                "INSERT INTO cycles(device_id, cycle_id, note, created_at) VALUES (?,?,?,?)",
                (device_id, cycle_id, note, now))
            return 201, {"cycle": {"device_id": device_id, "cycle_id": cycle_id,
                                   "note": note, "created_at": now},
                         "idempotent_replay": False}


def list_cycles(db_path: str, device_id: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        _get_device(conn, device_id)
        rows = conn.execute(
            """SELECT c.device_id, c.cycle_id, c.note, c.created_at, c.sealed_at,
                      (SELECT COUNT(*) FROM anchors a
                        WHERE a.device_id = c.device_id AND a.cycle_id = c.cycle_id) AS anchor_count,
                      (SELECT COUNT(*) FROM segments s
                        WHERE s.device_id = c.device_id AND s.cycle_id = c.cycle_id) AS segment_count
               FROM cycles c WHERE c.device_id=? ORDER BY c.cycle_id""",
            (device_id,)).fetchall()
        cycles = []
        for r in rows:
            d = dict(r)
            d["sealed"] = d["sealed_at"] is not None
            cycles.append(d)
        return 200, {"device_id": device_id, "cycles": cycles}


def get_cycle_detail(db_path: str, device_id: str, cycle_id: str):
    """周期详情：内容稳定指纹 + 封存状态。

    指纹由该周期当前全部锚点与声段内容生成；已封存周期返回封存时核对的
    最终指纹（内容已冻结，与实时重算一致）。
    """
    with db.session(db_path) as conn, db.read_txn(conn):
        _get_device(conn, device_id)
        cycle = _get_cycle(conn, device_id, cycle_id)
        anchors, segs = _cycle_contents(conn, device_id, cycle_id)
        sealed = cycle["sealed_at"] is not None
        fingerprint = (cycle["seal_fingerprint"] if sealed
                       else cycle_fingerprint(device_id, cycle_id, anchors, segs))
        return 200, {"cycle": {
            "device_id": device_id, "cycle_id": cycle_id,
            "note": cycle["note"], "created_at": cycle["created_at"],
            "anchor_count": len(anchors), "segment_count": len(segs),
            "sealed": sealed, "sealed_at": cycle["sealed_at"],
            "fingerprint": fingerprint}}


def seal_cycle(db_path: str, device_id: str, cycle_id: str, body):
    """封存设备的一个时钟周期（现场核对完成后执行，不可撤销）。

    - 请求必须携带从周期详情获取的内容指纹；在同一写事务内重算当前指纹并
      比对，不一致（内容已变化、指纹过期）-> 409，状态不变；
    - 该周期存在不可定位声段 -> 409，状态不变；
    - 生效后返回封存时间与最终指纹；重复相同封存请求返回首次结果（幂等）；
    - 封存后该周期的锚点/声段变更一律拒绝（见各写接口的 cycle_sealed）。
    """
    payload = require_payload(body)
    require_fields(payload, ["fingerprint"])
    fingerprint = validate_fingerprint(payload["fingerprint"])
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            cycle = _get_cycle(conn, device_id, cycle_id)
            if cycle["sealed_at"] is not None:
                # 已封存：相同指纹 -> 返回首次封存结果；不同指纹 -> 过期拒绝
                if fingerprint == cycle["seal_fingerprint"]:
                    return 200, {"cycle": {
                        "device_id": device_id, "cycle_id": cycle_id,
                        "sealed": True, "sealed_at": cycle["sealed_at"],
                        "fingerprint": cycle["seal_fingerprint"]},
                        "idempotent_replay": True}
                raise ApiError(
                    409, "seal_fingerprint_stale",
                    f"周期 {cycle_id} 已于 {cycle['sealed_at']} 封存，"
                    f"请求指纹与封存指纹不一致，拒绝重复封存",
                    {"device_id": device_id, "cycle_id": cycle_id,
                     "sealed_at": cycle["sealed_at"],
                     "sealed_fingerprint": cycle["seal_fingerprint"]})
            # 同一事务内核对当前内容：指纹过期（内容已被晚到记录改动）即拒绝
            anchors, segs = _cycle_contents(conn, device_id, cycle_id)
            current = cycle_fingerprint(device_id, cycle_id, anchors, segs)
            if fingerprint != current:
                raise ApiError(
                    409, "seal_fingerprint_stale",
                    f"请求指纹与设备 {device_id} 周期 {cycle_id} 的当前内容不一致"
                    f"（内容已变化，指纹过期），请重新获取周期详情后再封存",
                    {"device_id": device_id, "cycle_id": cycle_id,
                     "current_fingerprint": current})
            # 全部声段均可定位才允许封存
            pair = ([a["local_ms"] for a in anchors],
                    [a["utc_ms"] for a in anchors])
            unlocatable = [s["capture_id"] for s in segs
                           if project_segment(s, pair)["status"] == STATUS_UNLOCATABLE]
            if unlocatable:
                raise ApiError(
                    409, "seal_unlocatable_segments",
                    f"设备 {device_id} 周期 {cycle_id} 存在 {len(unlocatable)} 段"
                    f"无法定位的声段，须先补齐锚点或移除这些声段才能封存",
                    {"device_id": device_id, "cycle_id": cycle_id,
                     "unlocatable_capture_ids": unlocatable})
            now = iso_now()
            conn.execute(
                "UPDATE cycles SET sealed_at=?, seal_fingerprint=? "
                "WHERE device_id=? AND cycle_id=?",
                (now, current, device_id, cycle_id))
            return 201, {"cycle": {
                "device_id": device_id, "cycle_id": cycle_id,
                "sealed": True, "sealed_at": now,
                "fingerprint": current},
                "idempotent_replay": False}


# ---------------------------------------------------------------- 锚点

def list_anchors(db_path: str, device_id: str, cycle_id: str | None):
    with db.session(db_path) as conn, db.read_txn(conn):
        _get_device(conn, device_id)
        if cycle_id is not None:
            _get_cycle(conn, device_id, cycle_id)
        return 200, {"device_id": device_id,
                     "anchors": _anchors_of(conn, device_id, cycle_id)}


def add_anchor(db_path: str, device_id: str, body):
    payload = require_payload(body)
    require_fields(payload, ["local_ms", "utc_ms"])
    local_ms = validate_ms(payload["local_ms"], "local_ms")
    utc_ms = validate_ms(payload["utc_ms"], "utc_ms")
    cycle_id = validate_cycle_id(payload.get("cycle_id"))
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            cycle = _get_cycle(conn, device_id, cycle_id)
            _raise_if_sealed(cycle)
            _check_insert_increasing(_anchors_of(conn, device_id, cycle_id), local_ms, utc_ms)
            now = iso_now()
            conn.execute(
                "INSERT INTO anchors(device_id, cycle_id, local_ms, utc_ms, created_at) "
                "VALUES (?,?,?,?,?)",
                (device_id, cycle_id, local_ms, utc_ms, now))
            return 201, {"anchor": {"device_id": device_id, "cycle_id": cycle_id,
                                    "local_ms": local_ms, "utc_ms": utc_ms,
                                    "created_at": now}}


def replace_anchors(db_path: str, device_id: str, body):
    """锚点更正：按周期原子替换设备锚点。

    - 顶层可带 cycle_id（默认 default）；anchors 各项也可各自带 cycle_id，
      缺省项归入顶层周期；省略一切周期信息时行为与旧版一致（只替换 default）。
    - 每个被引用的周期独立做严格递增校验；任一项失败整体拒绝、不写入。
    - 仅替换"本次引用到的周期"的锚点，其他周期保持不变。
    """
    payload = require_payload(body)
    require_fields(payload, ["anchors"])
    items = payload["anchors"]
    if not isinstance(items, list):
        raise ApiError(400, "validation_error", "anchors 必须是数组",
                       {"received_type": type(items).__name__})
    top_cycle = validate_cycle_id(payload.get("cycle_id"))
    by_cycle: dict[str, list[tuple[int, int]]] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError(400, "validation_error", f"anchors[{i}] 必须是对象")
        require_fields(item, ["local_ms", "utc_ms"])
        cyc = validate_cycle_id(item.get("cycle_id", top_cycle))
        by_cycle.setdefault(cyc, []).append((
            validate_ms(item["local_ms"], f"anchors[{i}].local_ms"),
            validate_ms(item["utc_ms"], f"anchors[{i}].utc_ms")))
    # 逐周期校验（不写库，失败即整体拒绝）
    validated: dict[str, list[tuple[int, int]]] = {}
    for cyc, pairs in by_cycle.items():
        pairs.sort(key=lambda x: x[0])
        for i in range(1, len(pairs)):
            (pl, pu), (cl, cu) = pairs[i - 1], pairs[i]
            if cl == pl:
                raise ApiError(400, "anchors_not_increasing",
                               f"周期 {cyc} 内锚点本地时间重复：local_ms={cl}，"
                               f"锚点本地时间必须严格递增",
                               {"cycle_id": cyc, "local_ms": cl})
            if cu <= pu:
                raise ApiError(400, "anchors_not_increasing",
                               f"周期 {cyc} 内锚点 UTC 时间必须严格递增："
                               f"local_ms={pl} 的 utc_ms={pu}，local_ms={cl} 的 utc_ms={cu}",
                               {"cycle_id": cyc,
                                "prev": {"local_ms": pl, "utc_ms": pu},
                                "current": {"local_ms": cl, "utc_ms": cu}})
        validated[cyc] = pairs
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            # 任一引用周期已封存 -> 整体拒绝，其他周期不受影响（不写入）
            for cyc in validated:
                _raise_if_sealed(_get_cycle(conn, device_id, cyc))
            # 先删除所有被引用周期的旧锚点，再整批插入
            for cyc, pairs in validated.items():
                conn.execute("DELETE FROM anchors WHERE device_id=? AND cycle_id=?",
                             (device_id, cyc))
            now = iso_now()
            conn.executemany(
                "INSERT INTO anchors(device_id, cycle_id, local_ms, utc_ms, created_at) "
                "VALUES (?,?,?,?,?)",
                [(device_id, cyc, l, u, now) for cyc, pairs in validated.items()
                 for l, u in pairs])
            placeholders = ",".join("?" for _ in validated)
            affected = conn.execute(
                f"SELECT COUNT(*) AS c FROM segments "
                f"WHERE device_id=? AND cycle_id IN ({placeholders})",
                (device_id, *validated.keys())).fetchone()["c"]
            anchors = _anchors_of(conn, device_id)
    return 200, {"device_id": device_id, "cycle_ids": list(validated.keys()),
                 "anchors": anchors, "affected_segments": affected,
                 "message": "锚点已按周期整体替换，相关周期声段已按新锚点重算"}


def delete_anchor(db_path: str, device_id: str, local_ms: int, cycle_id: str):
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            cycle = _get_cycle(conn, device_id, cycle_id)
            _raise_if_sealed(cycle)
            cur = conn.execute(
                "DELETE FROM anchors WHERE device_id=? AND cycle_id=? AND local_ms=?",
                (device_id, cycle_id, local_ms))
            if cur.rowcount == 0:
                raise ApiError(404, "anchor_not_found",
                               f"设备 {device_id} 周期 {cycle_id} 不存在 local_ms={local_ms} 的锚点",
                               {"device_id": device_id, "cycle_id": cycle_id,
                                "local_ms": local_ms})
            affected = conn.execute(
                "SELECT COUNT(*) AS c FROM segments WHERE device_id=? AND cycle_id=?",
                (device_id, cycle_id)).fetchone()["c"]
            anchors = _anchors_of(conn, device_id)
    return 200, {"device_id": device_id, "cycle_id": cycle_id,
                 "deleted_local_ms": local_ms,
                 "anchors": anchors, "affected_segments": affected,
                 "message": "锚点已删除，该周期声段已按剩余锚点重算"}


# ---------------------------------------------------------------- 声段

def submit_segment(db_path: str, body):
    payload = require_payload(body)
    require_fields(payload, ["capture_id", "device_id", "local_start_ms", "local_end_ms"])
    capture_id = validate_id(payload["capture_id"], "capture_id")
    device_id = validate_id(payload["device_id"], "device_id")
    cycle_id = validate_cycle_id(payload.get("cycle_id"))
    start = validate_ms(payload["local_start_ms"], "local_start_ms")
    end = validate_ms(payload["local_end_ms"], "local_end_ms")
    note = validate_note(payload.get("note"))
    if start > end:
        raise ApiError(400, "segment_inverted",
                       f"声段起点 local_start_ms={start} 大于终点 local_end_ms={end}，"
                       f"起止倒置，拒绝写入",
                       {"local_start_ms": start, "local_end_ms": end})
    digest = segment_content_hash(capture_id, device_id, cycle_id, start, end, note)
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            _get_device(conn, device_id)
            cycle = _get_cycle(conn, device_id, cycle_id)
            row = conn.execute("SELECT * FROM segments WHERE capture_id=?",
                               (capture_id,)).fetchone()
            if row:
                # 同内容重传 / 异内容冲突：按现有幂等与冲突规则处理，
                # 即使所属周期已封存也不改变这两条规则（内容并未被修改）
                if row["content_hash"] == digest:
                    proj = project_segment(
                        _row_to_segment(row),
                        _anchors_map(conn).get((device_id, cycle_id), ([], [])))
                    return 200, {"segment": proj, "idempotent_replay": True}
                raise ApiError(409, "capture_conflict",
                               f"采集编号 {capture_id} 已存在且内容不一致，拒绝写入",
                               {"existing": _row_to_segment(row),
                                "received": {"capture_id": capture_id, "device_id": device_id,
                                             "cycle_id": cycle_id,
                                             "local_start_ms": start, "local_end_ms": end,
                                             "note": note}})
            # 新声段：所属周期已封存则拒绝写入
            _raise_if_sealed(cycle)
            now = iso_now()
            conn.execute(
                """INSERT INTO segments(capture_id, device_id, cycle_id,
                                        local_start_ms, local_end_ms,
                                        note, content_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (capture_id, device_id, cycle_id, start, end, note, digest, now))
            seg = {"capture_id": capture_id, "device_id": device_id,
                   "cycle_id": cycle_id,
                   "local_start_ms": start, "local_end_ms": end,
                   "note": note, "created_at": now}
            proj = project_segment(
                seg, _anchors_map(conn).get((device_id, cycle_id), ([], [])))
            return 201, {"segment": proj, "idempotent_replay": False}


def list_segments(db_path: str, device_id: str | None, cycle_id: str | None):
    with db.session(db_path) as conn, db.read_txn(conn):
        if device_id is not None:
            _get_device(conn, device_id)
            if cycle_id is not None:
                _get_cycle(conn, device_id, cycle_id)
                rows = conn.execute(
                    "SELECT * FROM segments WHERE device_id=? AND cycle_id=? ORDER BY capture_id",
                    (device_id, cycle_id)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM segments WHERE device_id=? ORDER BY capture_id",
                                    (device_id,)).fetchall()
        else:
            if cycle_id is not None:
                raise ApiError(400, "validation_error",
                               "按 cycle_id 过滤时必须同时提供 device_id",
                               {"cycle_id": cycle_id})
            rows = conn.execute("SELECT * FROM segments ORDER BY capture_id").fetchall()
        projs = _project_all(conn, rows)
        return 200, {"segments": projs, "count": len(projs)}


def get_segment(db_path: str, capture_id: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        row = conn.execute("SELECT * FROM segments WHERE capture_id=?",
                           (capture_id,)).fetchone()
        if not row:
            raise ApiError(404, "capture_not_found",
                           f"采集编号 {capture_id} 不存在", {"capture_id": capture_id})
        proj = project_segment(
            _row_to_segment(row),
            _anchors_map(conn).get((row["device_id"], row["cycle_id"]), ([], [])))
        return 200, {"segment": proj}


def delete_segment(db_path: str, capture_id: str):
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            row = conn.execute("SELECT * FROM segments WHERE capture_id=?",
                               (capture_id,)).fetchone()
            if not row:
                raise ApiError(404, "capture_not_found",
                               f"采集编号 {capture_id} 不存在", {"capture_id": capture_id})
            # 声段所属周期已封存 -> 拒绝删除
            _raise_if_sealed(_get_cycle(conn, row["device_id"], row["cycle_id"]))
            conn.execute("DELETE FROM segments WHERE capture_id=?", (capture_id,))
    return 200, {"deleted": capture_id}


# -------------------------------------------------------- 周期归属批量纠正

def reassign_segment_cycles(db_path: str, body):
    """批量纠正声段的周期归属（现场整理时发现声段被记到错误的时钟周期）。

    - 每项 {capture_id, from_cycle_id, to_cycle_id}：把声段从预期原周期移到
      目标周期；只能在同一设备的已登记周期之间移动（设备归属不变），
      起止本地毫秒、备注、创建时间与采集编号保持不变；
    - 任一项无效（编号不存在 / 同批重复 / 原周期与预期不符 / 目标周期未登记 /
      源或目标周期已封存 / 原周期等于目标周期）-> 整批拒绝，不改动任何数据；
    - 成功后声段内容指纹按新周期重算（同编号重传仍遵守同内容幂等、异内容
      冲突规则），返回各声段按目标周期锚点计算的当前定位结果（覆盖不足仍为
      不可定位），以及相关周期更新后的内容指纹；
    - 已发布版本保持原快照，之后发布的版本反映新归属。
    """
    payload = require_payload(body)
    require_fields(payload, ["items"])
    items = payload["items"]
    if not isinstance(items, list):
        raise ApiError(400, "validation_error", "items 必须是数组",
                       {"received_type": type(items).__name__})
    if not items:
        raise ApiError(400, "validation_error",
                       "items 不能为空：批量纠正至少包含一项")
    moves = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError(400, "validation_error", f"items[{i}] 必须是对象",
                           {"received_type": type(item).__name__})
        require_fields(item, ["capture_id", "from_cycle_id", "to_cycle_id"])
        moves.append({
            "capture_id": validate_id(item["capture_id"], f"items[{i}].capture_id"),
            "from_cycle_id": validate_id(item["from_cycle_id"],
                                         f"items[{i}].from_cycle_id"),
            "to_cycle_id": validate_id(item["to_cycle_id"], f"items[{i}].to_cycle_id"),
        })
    # 同批重复编号 -> 整批拒绝
    counts: dict[str, int] = {}
    for m in moves:
        counts[m["capture_id"]] = counts.get(m["capture_id"], 0) + 1
    dups = sorted(c for c, n in counts.items() if n > 1)
    if dups:
        raise ApiError(409, "reassign_duplicate",
                       f"同批包含重复的采集编号: {', '.join(dups)}，"
                       f"每个采集编号每批只能出现一次，整批拒绝",
                       {"capture_ids": dups})
    # 原周期等于目标周期的项不构成纠正 -> 整批拒绝
    same = sorted({m["capture_id"] for m in moves
                   if m["from_cycle_id"] == m["to_cycle_id"]})
    if same:
        raise ApiError(400, "reassign_same_cycle",
                       f"采集编号 {', '.join(same)} 的原周期与目标周期相同，"
                       f"不构成纠正，整批拒绝",
                       {"capture_ids": same})
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            # 1) 任一编号不存在 -> 整批拒绝
            rows = {}
            missing = []
            for m in moves:
                cid = m["capture_id"]
                row = conn.execute("SELECT * FROM segments WHERE capture_id=?",
                                   (cid,)).fetchone()
                if row is None:
                    missing.append(cid)
                else:
                    rows[cid] = row
            if missing:
                raise ApiError(404, "capture_not_found",
                               f"采集编号不存在: {', '.join(sorted(missing))}，"
                               f"整批拒绝，不改动任何数据",
                               {"capture_ids": sorted(missing)})
            # 2) 原周期与预期不符 -> 整批拒绝
            mismatches = [
                {"capture_id": m["capture_id"],
                 "device_id": rows[m["capture_id"]]["device_id"],
                 "expected_cycle_id": m["from_cycle_id"],
                 "actual_cycle_id": rows[m["capture_id"]]["cycle_id"]}
                for m in moves
                if rows[m["capture_id"]]["cycle_id"] != m["from_cycle_id"]]
            if mismatches:
                raise ApiError(409, "reassign_cycle_mismatch",
                               f"{len(mismatches)} 项的原周期与预期不符"
                               f"（声段当前周期与 from_cycle_id 不一致），"
                               f"整批拒绝，不改动任何数据",
                               {"mismatches": mismatches})
            # 3) 目标周期必须在声段所属设备上已登记 -> 否则整批拒绝
            cycle_rows: dict = {}

            def cycle_of(device_id, cycle_id):
                key = (device_id, cycle_id)
                if key not in cycle_rows:
                    r = conn.execute(
                        "SELECT device_id, cycle_id, sealed_at FROM cycles "
                        "WHERE device_id=? AND cycle_id=?", key).fetchone()
                    cycle_rows[key] = dict(r) if r is not None else None
                return cycle_rows[key]

            missing_cycles = [
                {"capture_id": m["capture_id"],
                 "device_id": rows[m["capture_id"]]["device_id"],
                 "cycle_id": m["to_cycle_id"]}
                for m in moves
                if cycle_of(rows[m["capture_id"]]["device_id"],
                            m["to_cycle_id"]) is None]
            if missing_cycles:
                raise ApiError(404, "cycle_not_found",
                               f"目标周期未在声段所属设备上登记：只能在同一设备的"
                               f"已登记周期之间移动，整批拒绝，不改动任何数据",
                               {"items": missing_cycles})
            # 4) 源/目标周期已封存 -> 整批拒绝
            sealed, seen_sealed = [], set()
            for m in moves:
                dev = rows[m["capture_id"]]["device_id"]
                for cyc_id in (m["from_cycle_id"], m["to_cycle_id"]):
                    cyc = cycle_of(dev, cyc_id)
                    # 源周期必然已登记（声段提交时校验过，且无删除周期入口）
                    if (cyc is not None and cyc["sealed_at"] is not None
                            and (dev, cyc_id) not in seen_sealed):
                        seen_sealed.add((dev, cyc_id))
                        sealed.append({"device_id": dev, "cycle_id": cyc_id,
                                       "sealed_at": cyc["sealed_at"]})
            if sealed:
                raise ApiError(409, "cycle_sealed",
                               f"源/目标周期已封存，周期归属已冻结，"
                               f"整批拒绝，不改动任何数据",
                               {"cycles": sealed})
            # 5) 全部通过：逐条移动（仅改周期归属与内容指纹，其余字段保持不变）
            for m in moves:
                row = rows[m["capture_id"]]
                digest = segment_content_hash(
                    m["capture_id"], row["device_id"], m["to_cycle_id"],
                    row["local_start_ms"], row["local_end_ms"], row["note"])
                conn.execute(
                    "UPDATE segments SET cycle_id=?, content_hash=? "
                    "WHERE capture_id=?",
                    (m["to_cycle_id"], digest, m["capture_id"]))
            # 6) 按目标周期锚点计算各声段当前定位结果（同事务读取；
            #    目标周期覆盖不足的声段仍为不可定位）
            anchors = _anchors_map(conn)
            projs = sort_projections([
                project_segment(
                    {**_row_to_segment(rows[m["capture_id"]]),
                     "cycle_id": m["to_cycle_id"]},
                    anchors.get((rows[m["capture_id"]]["device_id"],
                                 m["to_cycle_id"]), ([], [])))
                for m in moves])
            # 7) 相关周期（源+目标）内容指纹随移动更新，一并返回便于核对
            affected = sorted(
                {(rows[m["capture_id"]]["device_id"], m["from_cycle_id"])
                 for m in moves}
                | {(rows[m["capture_id"]]["device_id"], m["to_cycle_id"])
                   for m in moves})
            cycle_fps = [{"device_id": dev, "cycle_id": cyc,
                          "fingerprint": _cycle_content_fingerprint(conn, dev, cyc)}
                         for dev, cyc in affected]
    return 200, {"moved": len(moves), "segments": projs, "cycles": cycle_fps,
                 "message": "周期归属已批量纠正：声段起止、备注、创建时间与采集编号"
                            "保持不变，已按目标周期锚点重算定位"}


# ---------------------------------------------------------------- 当前时间轴

def current_timeline(db_path: str):
    """当前（未发布）时间轴：按现有锚点实时重算，显示周期归属。"""
    with db.session(db_path) as conn, db.read_txn(conn):
        devices = [dict(r) for r in conn.execute(
            "SELECT device_id, name, created_at FROM devices ORDER BY device_id")]
        cycles_rows = conn.execute(
            "SELECT device_id, cycle_id, note, created_at FROM cycles "
            "ORDER BY device_id, cycle_id").fetchall()
        anchor_rows = conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms FROM anchors "
            "ORDER BY device_id, cycle_id, local_ms"
        ).fetchall()
        cyc_by_dev: dict[str, list] = {}
        for r in cycles_rows:
            cyc_by_dev.setdefault(r["device_id"], []).append(dict(r))
        anc_by_dev: dict[str, list] = {}
        for r in anchor_rows:
            anc_by_dev.setdefault(r["device_id"], []).append(
                {"cycle_id": r["cycle_id"], "local_ms": r["local_ms"],
                 "utc_ms": r["utc_ms"]})
        for d in devices:
            d["cycles"] = cyc_by_dev.get(d["device_id"], [])
            d["anchors"] = anc_by_dev.get(d["device_id"], [])
        rows = conn.execute("SELECT * FROM segments ORDER BY capture_id").fetchall()
        projs = _project_all(conn, rows)
        return 200, {"generated_at": iso_now(), "devices": devices,
                     "segments": projs, "segment_count": len(projs)}


# ---------------------------------------------------------------- 版本发布

def _version_doc(version_no, note, created_at, anchor_rows, projs, changes, prev_no,
                 format_rev):
    doc = {
        "version_no": version_no,
        "note": note,
        "created_at": created_at,
        "based_on_version": prev_no,
        "anchors": [{"device_id": r["device_id"], "cycle_id": r["cycle_id"],
                     "local_ms": r["local_ms"], "utc_ms": r["utc_ms"]}
                    for r in anchor_rows],
        "segments": projs,
        "segment_count": len(projs),
        "changes": dict(changes, from_version=prev_no),
    }
    if format_rev >= SCHEMA_VERSION:
        doc["format_rev"] = format_rev
    return doc


def publish_version(db_path: str, body):
    """发布新版本：在单个写事务内读取全部锚点与声段、计算投影并落盘快照。

    与锚点更正并发时，写锁保证二者串行，因此每个版本都对应一套
    完整且自洽的（分周期）锚点 + 声段快照；已发布版本此后不可变。
    新发布版本按 format_rev=2 原子冻结各周期锚点与声段投影。
    """
    note = None
    if body is not None:
        note = validate_note(require_payload(body).get("note"))
    with db.session(db_path) as conn:
        with db.write_txn(conn):
            seg_rows = conn.execute("SELECT * FROM segments ORDER BY capture_id").fetchall()
            projs = _project_all(conn, seg_rows)
            anchor_rows = conn.execute(
                "SELECT device_id, cycle_id, local_ms, utc_ms FROM anchors "
                "ORDER BY device_id, cycle_id, local_ms"
            ).fetchall()

            prev = conn.execute(
                "SELECT version_no, format_rev FROM versions "
                "ORDER BY version_no DESC LIMIT 1").fetchone()
            prev_no = prev["version_no"] if prev else None
            prev_projs = []
            if prev_no is not None:
                prev_projs = [_version_row_to_proj(r) for r in conn.execute(
                    "SELECT * FROM version_segments WHERE version_no=?", (prev_no,))]

            now = iso_now()
            conn.execute(
                "INSERT INTO versions(version_no, note, created_at, format_rev) "
                "VALUES (?,?,?,?)",
                (prev_no + 1 if prev_no is not None else 1, note, now, SCHEMA_VERSION))
            new_no = 1 if prev_no is None else prev_no + 1
            conn.executemany(
                "INSERT INTO version_anchors(version_no, device_id, cycle_id, local_ms, utc_ms) "
                "VALUES (?,?,?,?,?)",
                [(new_no, r["device_id"], r["cycle_id"], r["local_ms"], r["utc_ms"])
                 for r in anchor_rows])
            conn.executemany(
                """INSERT INTO version_segments(version_no, capture_id, device_id, cycle_id,
                                                local_start_ms, local_end_ms, note,
                                                status, utc_start_ms, utc_end_ms, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(new_no, p["capture_id"], p["device_id"], p["cycle_id"],
                  p["local_start_ms"], p["local_end_ms"], p["note"], p["status"],
                  p["utc_start_ms"], p["utc_end_ms"], p["created_at"]) for p in projs])
            changes = compute_changes(prev_projs, projs)
            return 201, _version_doc(new_no, note, now, anchor_rows, projs, changes,
                                     prev_no, SCHEMA_VERSION)


def list_versions(db_path: str):
    with db.session(db_path) as conn, db.read_txn(conn):
        rows = conn.execute(
            """SELECT v.version_no, v.note, v.created_at,
                      (SELECT COUNT(*) FROM version_segments s WHERE s.version_no = v.version_no) AS segment_count,
                      (SELECT COUNT(*) FROM version_anchors  a WHERE a.version_no = v.version_no) AS anchor_count
               FROM versions v ORDER BY v.version_no""").fetchall()
        return 200, {"versions": [dict(r) for r in rows]}


def get_version(db_path: str, version_no: int):
    with db.session(db_path) as conn, db.read_txn(conn):
        v = conn.execute(
            "SELECT version_no, note, created_at, format_rev FROM versions WHERE version_no=?",
            (version_no,)).fetchone()
        if not v:
            raise ApiError(404, "version_not_found",
                           f"版本 {version_no} 不存在", {"version_no": version_no})
        format_rev = v["format_rev"]
        anchor_rows = conn.execute(
            "SELECT device_id, cycle_id, local_ms, utc_ms FROM version_anchors "
            "WHERE version_no=? ORDER BY device_id, cycle_id, local_ms",
            (version_no,)).fetchall()
        projs = sort_projections([_version_row_to_proj(r) for r in conn.execute(
            "SELECT * FROM version_segments WHERE version_no=?", (version_no,))])
        prev_row = conn.execute(
            "SELECT MAX(version_no) AS v FROM versions WHERE version_no < ?",
            (version_no,)).fetchone()
        prev_no = prev_row["v"]
        prev_projs = []
        if prev_no is not None:
            prev_projs = [_version_row_to_proj(r) for r in conn.execute(
                "SELECT * FROM version_segments WHERE version_no=?", (prev_no,))]
        changes = compute_changes(prev_projs, projs)
        doc = _version_doc(version_no, v["note"], v["created_at"],
                           anchor_rows, projs, changes, prev_no, format_rev)
        if format_rev < SCHEMA_VERSION:
            # 已发布旧版本：剔除周期字段，查询结果与升级前完全一致
            doc = _strip_cycle_fields(doc)
        return 200, doc
