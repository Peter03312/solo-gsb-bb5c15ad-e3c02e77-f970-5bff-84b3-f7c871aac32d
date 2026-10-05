"""端到端测试：周期封存（seal，v1.2）。

覆盖：
- 周期详情返回内容稳定指纹与封存状态；指纹随锚点/声段内容变化；
- 封存成功：携带当前指纹且全部声段可定位 -> 返回封存时间与最终指纹；
- 重复相同封存请求 -> 返回首次结果（幂等）；封存不可撤销（无解除入口）；
- 指纹过期 / 存在不可定位声段 -> 拒绝且状态不变；
- 封存后该周期锚点新增/更正/删除、声段新增/删除一律 409 cycle_sealed；
  同内容声段重传仍按幂等规则返回、异内容仍按 capture_conflict 处理；
- 锚点更正涉及封存周期 -> 整体拒绝，其他周期不受影响；
- 已发布版本与未封存周期行为不变；旧库迁移后周期默认未封存、可正常封存。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402

# v1（无周期、无封存列）建表脚本，仅用于构造待迁移的旧库
LEGACY_SCHEMA = """
CREATE TABLE devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE anchors (
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, local_ms)
);
CREATE TABLE segments (
    capture_id      TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE versions (
    version_no  INTEGER PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE version_anchors (
    version_no  INTEGER NOT NULL,
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, local_ms)
);
CREATE TABLE version_segments (
    version_no      INTEGER NOT NULL,
    capture_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL,
    utc_start_ms    INTEGER,
    utc_end_ms      INTEGER,
    created_at      TEXT,
    PRIMARY KEY (version_no, capture_id)
);
"""


def request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class SealApiTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    # ------------------------------------------------------------ 工具

    def make_device(self, device_id="D1", name=None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", "/devices", payload)

    def reg_cycle(self, device_id, cycle_id, note=None):
        payload = {"cycle_id": cycle_id}
        if note is not None:
            payload["note"] = note
        return self.req("POST", f"/devices/{device_id}/cycles", payload)

    def add_anchor(self, device_id, local_ms, utc_ms, cycle_id=None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self.req("POST", f"/devices/{device_id}/anchors", payload)

    def submit(self, capture_id, device_id, start, end, cycle_id=None, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": start, "local_end_ms": end}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def cycle_detail(self, device_id, cycle_id):
        return self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")

    def fingerprint(self, device_id, cycle_id):
        status, body = self.cycle_detail(device_id, cycle_id)
        self.assertEqual(status, 200)
        return body["cycle"]["fingerprint"]

    def seal(self, device_id, cycle_id, fingerprint):
        return self.req("POST", f"/devices/{device_id}/cycles/{cycle_id}/seal",
                        {"fingerprint": fingerprint})

    def make_located_cycle(self, device_id="D1", cycle_id=None):
        """建一个有两只锚点、一段可定位声段的周期，返回其指纹。"""
        self.make_device(device_id)
        self.add_anchor(device_id, 1000, 100000, cycle_id)
        self.add_anchor(device_id, 2000, 101000, cycle_id)
        self.submit("CAP-1", device_id, 1100, 1200, cycle_id)
        return self.fingerprint(device_id, cycle_id or "default")

    def seal_default(self, device_id="D1", cycle_id="default"):
        fp = self.fingerprint(device_id, cycle_id)
        status, body = self.seal(device_id, cycle_id, fp)
        self.assertEqual(status, 201)
        return body

    # ------------------------------------------------------------ 周期详情

    def test_cycle_detail_returns_fingerprint_and_seal_state(self):
        self.make_device()
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.submit("CAP-1", "D1", 1100, 1200, note="鸟鸣")

        status, body = self.cycle_detail("D1", "default")
        self.assertEqual(status, 200)
        cyc = body["cycle"]
        self.assertEqual(cyc["device_id"], "D1")
        self.assertEqual(cyc["cycle_id"], "default")
        self.assertEqual(cyc["anchor_count"], 2)
        self.assertEqual(cyc["segment_count"], 1)
        self.assertFalse(cyc["sealed"])
        self.assertIsNone(cyc["sealed_at"])
        self.assertRegex(cyc["fingerprint"], r"^[0-9a-f]{64}$")

        # 周期列表也带封存状态
        _, body = self.req("GET", "/devices/D1/cycles")
        self.assertEqual(body["cycles"][0]["sealed"], False)
        self.assertIsNone(body["cycles"][0]["sealed_at"])

    def test_cycle_detail_not_found(self):
        self.make_device()
        status, body = self.cycle_detail("D1", "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        status, body = self.cycle_detail("NOPE", "default")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

    def test_fingerprint_stable_and_content_sensitive(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.submit("CAP-1", "D1", 1100, 1200)

        fp1 = self.fingerprint("D1", "default")
        # 同样内容重复读取 -> 指纹稳定
        self.assertEqual(self.fingerprint("D1", "default"), fp1)
        # 空周期指纹与有内容周期不同
        self.assertNotEqual(self.fingerprint("D1", "boot-2"), fp1)

        # 声段新增 -> 指纹变化
        self.submit("CAP-2", "D1", 1300, 1400)
        fp2 = self.fingerprint("D1", "default")
        self.assertNotEqual(fp2, fp1)
        # 锚点新增 -> 指纹变化
        self.add_anchor("D1", 3000, 102000)
        fp3 = self.fingerprint("D1", "default")
        self.assertNotEqual(fp3, fp2)
        # 声段删除 -> 指纹变化
        self.req("DELETE", "/segments/CAP-2")
        self.assertNotEqual(self.fingerprint("D1", "default"), fp3)
        # 锚点更正 -> 指纹变化
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 1000, "utc_ms": 100000},
                              {"local_ms": 2000, "utc_ms": 101001}]})
        self.assertNotEqual(self.fingerprint("D1", "default"), fp1)
        # 其他周期（boot-2）内容未动 -> 指纹不受 default 变更影响
        fp_b2 = self.fingerprint("D1", "boot-2")
        self.submit("CAP-3", "D1", 1500, 1600)
        self.assertEqual(self.fingerprint("D1", "boot-2"), fp_b2)

    # ------------------------------------------------------------ 封存成功与幂等

    def test_seal_success_returns_time_and_final_fingerprint(self):
        fp = self.make_located_cycle()
        status, body = self.seal("D1", "default", fp)
        self.assertEqual(status, 201)
        self.assertFalse(body["idempotent_replay"])
        cyc = body["cycle"]
        self.assertTrue(cyc["sealed"])
        self.assertEqual(cyc["fingerprint"], fp)  # 最终指纹 == 核对时指纹
        self.assertTrue(cyc["sealed_at"].endswith("Z"))  # 封存时间

        # 详情显示已封存，指纹保持封存时的最终指纹
        _, detail = self.cycle_detail("D1", "default")
        self.assertTrue(detail["cycle"]["sealed"])
        self.assertEqual(detail["cycle"]["sealed_at"], cyc["sealed_at"])
        self.assertEqual(detail["cycle"]["fingerprint"], fp)
        # 周期列表同步显示
        _, lst = self.req("GET", "/devices/D1/cycles")
        self.assertTrue(lst["cycles"][0]["sealed"])
        self.assertEqual(lst["cycles"][0]["sealed_at"], cyc["sealed_at"])

    def test_seal_replay_returns_first_result(self):
        fp = self.make_located_cycle()
        status, first = self.seal("D1", "default", fp)
        self.assertEqual(status, 201)
        # 重复相同封存请求 -> 返回首次结果（封存时间不变）
        status, again = self.seal("D1", "default", fp)
        self.assertEqual(status, 200)
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["cycle"], first["cycle"])

    def test_seal_irreversible(self):
        fp = self.make_located_cycle()
        self.seal("D1", "default", fp)
        # 无解除封存入口
        status, _ = self.req("POST", "/devices/D1/cycles/default/unseal", {})
        self.assertEqual(status, 404)
        status, body = self.req("DELETE", "/devices/D1/cycles/default")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        # 已封存周期用不同指纹再请求 -> 拒绝，封存状态不变
        status, body = self.seal("D1", "default", "0" * 64)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "seal_fingerprint_stale")
        _, detail = self.cycle_detail("D1", "default")
        self.assertTrue(detail["cycle"]["sealed"])
        self.assertEqual(detail["cycle"]["fingerprint"], fp)

    # ------------------------------------------------------------ 拒绝场景

    def test_seal_stale_fingerprint_rejected_state_unchanged(self):
        fp = self.make_located_cycle()
        # 取到指纹后又有晚到记录改动内容
        self.submit("CAP-2", "D1", 1300, 1400)
        status, body = self.seal("D1", "default", fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "seal_fingerprint_stale")
        self.assertEqual(body["error"]["details"]["current_fingerprint"],
                         self.fingerprint("D1", "default"))
        # 状态不变：仍未封存
        _, detail = self.cycle_detail("D1", "default")
        self.assertFalse(detail["cycle"]["sealed"])
        # 用新指纹可以封存成功
        status, _ = self.seal("D1", "default", self.fingerprint("D1", "default"))
        self.assertEqual(status, 201)

    def test_seal_unlocatable_segments_rejected_state_unchanged(self):
        self.make_device()
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        self.submit("CAP-OK", "D1", 1100, 1200)
        self.submit("CAP-OUT", "D1", 9000, 9500)  # 越界 -> 不可定位
        fp = self.fingerprint("D1", "default")
        status, body = self.seal("D1", "default", fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "seal_unlocatable_segments")
        self.assertEqual(body["error"]["details"]["unlocatable_capture_ids"],
                         ["CAP-OUT"])
        # 状态不变：仍未封存
        _, detail = self.cycle_detail("D1", "default")
        self.assertFalse(detail["cycle"]["sealed"])
        # 移除不可定位声段后，重新取指纹可封存
        self.req("DELETE", "/segments/CAP-OUT")
        status, _ = self.seal("D1", "default", self.fingerprint("D1", "default"))
        self.assertEqual(status, 201)

    def test_seal_validation_errors(self):
        self.make_device()
        # 缺 fingerprint 字段
        status, body = self.req("POST", "/devices/D1/cycles/default/seal", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        # 指纹格式非法
        status, body = self.seal("D1", "default", "not-a-fingerprint")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        # 周期不存在 / 设备不存在
        status, body = self.seal("D1", "ghost", "0" * 64)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        status, body = self.seal("NOPE", "default", "0" * 64)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

    # ------------------------------------------------------------ 封存后的写保护

    def test_sealed_cycle_rejects_anchor_mutations(self):
        self.make_located_cycle()
        self.seal_default()
        # 新增锚点 -> 409
        status, body = self.add_anchor("D1", 3000, 102000)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 更正（整体替换）-> 409
        status, body = self.req("PUT", "/devices/D1/anchors",
                                {"anchors": [{"local_ms": 1000, "utc_ms": 100001}]})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 删除锚点 -> 409
        status, body = self.req("DELETE", "/devices/D1/anchors/1000")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 锚点保持封存时内容
        anchors = self.req("GET", "/devices/D1/anchors")[1]["anchors"]
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in anchors],
                         [(1000, 100000), (2000, 101000)])

    def test_sealed_cycle_rejects_segment_mutations(self):
        self.make_located_cycle()
        self.seal_default()
        # 新增声段 -> 409
        status, body = self.submit("CAP-2", "D1", 1300, 1400)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 删除声段 -> 409
        status, body = self.req("DELETE", "/segments/CAP-1")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 声段保持封存时内容
        segs = self.req("GET", "/segments")[1]
        self.assertEqual([s["capture_id"] for s in segs["segments"]], ["CAP-1"])

    def test_sealed_cycle_segment_replay_and_conflict_keep_existing_rules(self):
        self.make_located_cycle()
        self.seal_default()
        # 同内容重传 -> 仍按现有幂等规则返回（不视为变更）
        status, body = self.submit("CAP-1", "D1", 1100, 1200)
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        self.assertEqual(body["segment"]["utc_start_ms"], 100100)
        # 异内容重传 -> 仍按现有冲突规则处理
        status, body = self.submit("CAP-1", "D1", 1100, 1201)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        # 均不产生写入
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 1)

    def test_put_anchors_touching_sealed_cycle_rejected_atomically(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 2000, 101000, "default")
        self.add_anchor("D1", 1000, 200000, "boot-2")
        self.add_anchor("D1", 2000, 202000, "boot-2")
        self.submit("CAP-D", "D1", 1100, 1200, "default")
        self.seal_default()

        # 一次更正同时涉及封存周期与未封存周期 -> 整体拒绝
        status, body = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 200500},
            {"cycle_id": "boot-2", "local_ms": 2000, "utc_ms": 202500},
            {"cycle_id": "default", "local_ms": 1000, "utc_ms": 100500},
        ]})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(body["error"]["details"]["cycle_id"], "default")
        # 其他周期不受影响：boot-2 锚点保持原样（整体未写入）
        anchors = self.req("GET", "/devices/D1/anchors?cycle_id=boot-2")[1]["anchors"]
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in anchors],
                         [(1000, 200000), (2000, 202000)])
        # 只更正未封存周期 -> 正常生效
        status, body = self.req("PUT", "/devices/D1/anchors", {
            "cycle_id": "boot-2",
            "anchors": [{"local_ms": 1000, "utc_ms": 200500},
                        {"local_ms": 2000, "utc_ms": 202500}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_ids"], ["boot-2"])

    def test_unsealed_cycle_and_other_device_unaffected(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 2000, 101000, "default")
        self.submit("CAP-D", "D1", 1100, 1200, "default")
        self.seal_default()

        # 同设备未封存周期：一切写操作正常
        self.assertEqual(self.add_anchor("D1", 1000, 200000, "boot-2")[0], 201)
        self.assertEqual(self.add_anchor("D1", 2000, 202000, "boot-2")[0], 201)
        self.assertEqual(self.submit("CAP-B", "D1", 1100, 1200, "boot-2")[0], 201)
        self.assertEqual(self.req("DELETE", "/segments/CAP-B")[0], 200)
        self.assertEqual(
            self.req("DELETE", "/devices/D1/anchors/2000?cycle_id=boot-2")[0], 200)
        # 其他设备同编号周期不受牵连
        self.make_device("D2")
        self.assertEqual(self.add_anchor("D2", 1000, 100000)[0], 201)
        self.assertEqual(self.submit("CAP-2", "D2", 1000, 1500)[0], 201)

    # ------------------------------------------------------------ 与版本发布共存

    def test_publish_and_versions_unaffected_by_seal(self):
        self.make_located_cycle()
        # 发布 v1（未封存）
        status, v1 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.seal_default()
        # 封存后发布 v2：内容冻结 -> 无位移/新增/移除
        status, v2 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v2["changes"]["added"], [])
        self.assertEqual(v2["changes"]["removed"], [])
        self.assertEqual(v2["changes"]["displaced"], [])
        # 已发布版本查询结果不变
        _, v1_again = self.req("GET", "/versions/1")
        self.assertEqual(v1_again["segments"], v1["segments"])
        self.assertEqual(v1_again["anchors"], v1["anchors"])

    def test_seal_non_default_cycle(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 200000, "boot-2")
        self.add_anchor("D1", 2000, 202000, "boot-2")
        self.submit("CAP-B", "D1", 1100, 1200, "boot-2")
        fp = self.fingerprint("D1", "boot-2")
        status, body = self.seal("D1", "boot-2", fp)
        self.assertEqual(status, 201)
        # boot-2 已封存，default 未封存
        status, body = self.add_anchor("D1", 3000, 203000, "boot-2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(self.add_anchor("D1", 1000, 100000, "default")[0], 201)


# ------------------------------------------------------------ 旧库迁移

class SealMigrationTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        self._seed_legacy()
        db.init_db(self.db_path)  # 在线迁移（v1 -> 周期版 -> 补封存列）
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    def _seed_legacy(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute("INSERT INTO devices VALUES ('D1','录音笔','2026-09-01T00:00:00.000Z')")
            conn.execute("INSERT INTO anchors VALUES ('D1',1000,100000,'t')")
            conn.execute("INSERT INTO anchors VALUES ('D1',2000,101000,'t')")
            import hashlib
            import json as _json
            canon = _json.dumps(
                {"capture_id": "OLD1", "device_id": "D1",
                 "local_start_ms": 1100, "local_end_ms": 1200, "note": None},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO segments VALUES ('OLD1','D1',1100,1200,NULL,?,'t')",
                (digest,))
            conn.commit()
        finally:
            conn.close()

    def test_legacy_cycles_default_unsealed(self):
        # 旧库迁移而来（含 v1 迁移路径补建的 cycles 表）：默认未封存
        _, body = self.req("GET", "/devices/D1/cycles")
        self.assertEqual(len(body["cycles"]), 1)
        self.assertFalse(body["cycles"][0]["sealed"])
        self.assertIsNone(body["cycles"][0]["sealed_at"])
        # 详情可用，指纹稳定
        _, detail = self.req("GET", "/devices/D1/cycles/default")
        self.assertFalse(detail["cycle"]["sealed"])
        self.assertRegex(detail["cycle"]["fingerprint"], r"^[0-9a-f]{64}$")

    def test_legacy_cycle_sealable_after_migration(self):
        # 迁移后的旧周期可正常封存，封存后写保护生效
        _, detail = self.req("GET", "/devices/D1/cycles/default")
        fp = detail["cycle"]["fingerprint"]
        status, body = self.req("POST", "/devices/D1/cycles/default/seal",
                                {"fingerprint": fp})
        self.assertEqual(status, 201)
        self.assertTrue(body["cycle"]["sealed"])
        status, body = self.req("POST", "/devices/D1/anchors",
                                {"local_ms": 3000, "utc_ms": 102000})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 旧声段同内容重传仍幂等
        status, body = self.req("POST", "/segments", {
            "capture_id": "OLD1", "device_id": "D1",
            "local_start_ms": 1100, "local_end_ms": 1200})
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
