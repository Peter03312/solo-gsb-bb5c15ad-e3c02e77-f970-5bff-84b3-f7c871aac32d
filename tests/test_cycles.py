"""端到端测试：设备时钟周期（cycle）能力。

覆盖：
- 周期登记（幂等/冲突/设备不存在）；
- 默认周期与已有数据的默认归属；
- 同设备不同周期复用本地毫秒，各周期独立递增校验、独立线性映射；
- 声段仅用所属周期锚点，覆盖不足仍为 unlocatable；周期不存在拒绝且不写入；
- 采集编号全局唯一，内容指纹把周期计入（跨周期改周期 = 冲突，同周期同内容幂等）；
- 当前时间轴与新发布版本显示 cycle_id；发布原子冻结各周期；
- 旧 v1 库在线迁移：已有数据归 default，已发布旧版本查询结果完全不变。

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

# v1（无周期）建表脚本，仅用于构造待迁移的旧库
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


class CycleApiTestCase(unittest.TestCase):
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

    # ------------------------------------------------------------ 周期登记

    def test_device_has_default_cycle(self):
        self.make_device("D1")
        status, body = self.req("GET", "/devices/D1/cycles")
        self.assertEqual(status, 200)
        ids = [c["cycle_id"] for c in body["cycles"]]
        self.assertEqual(ids, ["default"])

    def test_register_cycle_idempotent_and_conflict(self):
        self.make_device()
        status, body = self.reg_cycle("D1", "boot-2", note="第二次开机")
        self.assertEqual(status, 201)
        self.assertFalse(body["idempotent_replay"])
        # 同内容重登 -> 幂等
        status, body = self.reg_cycle("D1", "boot-2", note="第二次开机")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        # note 不一致 -> 冲突
        status, body = self.reg_cycle("D1", "boot-2", note="别的备注")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_conflict")

    def test_register_cycle_device_not_found(self):
        status, body = self.req("POST", "/devices/NOPE/cycles", {"cycle_id": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

    # ------------------------------------------------------------ 本地毫秒复用

    def test_local_ms_reusable_across_cycles(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        # 两个周期都在 local_ms=1000 处放锚点，互不冲突
        self.assertEqual(self.add_anchor("D1", 1000, 100000, "default")[0], 201)
        self.assertEqual(self.add_anchor("D1", 1000, 200000, "boot-2")[0], 201)
        status, body = self.req("GET", "/devices/D1/anchors")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["anchors"]), 2)
        for a in body["anchors"]:
            self.assertEqual(a["local_ms"], 1000)
        self.assertEqual({a["cycle_id"] for a in body["anchors"]},
                         {"default", "boot-2"})
        # 同一周期内仍不允许重复
        status, body = self.add_anchor("D1", 1000, 100001, "default")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")

    def test_anchor_to_unknown_cycle_rejected(self):
        self.make_device()
        status, body = self.add_anchor("D1", 1000, 100000, "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        # 未写入
        status, body = self.req("GET", "/devices/D1/anchors")
        self.assertEqual(body["anchors"], [])

    def test_increasing_check_is_per_cycle(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        # 各自周期内严格递增；跨周期互不影响
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 2000, 101000, "default")
        self.add_anchor("D1", 1000, 200000, "boot-2")
        self.add_anchor("D1", 2000, 202000, "boot-2")
        # default 周期在中间插入 UTC 不递增 -> 仅与 default 锚点比较，拒绝
        status, body = self.add_anchor("D1", 1500, 99999, "default")
        self.assertEqual(status, 400)
        # 同样的 UTC 在 boot-2 周期合法（其后锚点 utc=202000）
        status, _ = self.add_anchor("D1", 1500, 201000, "boot-2")
        self.assertEqual(status, 201)

    # ------------------------------------------------------------ 声段按周期映射

    def test_segment_uses_own_cycle_anchors(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        # default 周期：local 1000->utc 100000, 2000->101000（斜率1.0）
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 2000, 101000, "default")
        # boot-2 周期：local 1000->utc 200000, 2000->202000（斜率2.0）
        self.add_anchor("D1", 1000, 200000, "boot-2")
        self.add_anchor("D1", 2000, 202000, "boot-2")

        # 同 local 区间的两段声段，仅用各自周期锚点
        _, b_def = self.submit("C-DEF", "D1", 1100, 1200, "default")
        _, b_new = self.submit("C-B2", "D1", 1100, 1200, "boot-2")
        self.assertEqual(b_def["segment"]["utc_start_ms"], 100100)
        self.assertEqual(b_new["segment"]["utc_start_ms"], 200200)
        self.assertEqual(b_new["segment"]["cycle_id"], "boot-2")
        self.assertEqual(b_def["segment"]["cycle_id"], "default")

    def test_segment_unknown_cycle_rejected_not_written(self):
        self.make_device()
        status, body = self.submit("C1", "D1", 0, 10, "ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        status, body = self.req("GET", "/segments")
        self.assertEqual(body["count"], 0)

    def test_segment_coverage_per_cycle_unlocatable(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        # 只有 default 周期有锚点
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 2000, 101000, "default")
        # boot-2 无锚点 -> 即便 local 值落在 default 覆盖区间，也不可定位
        _, body = self.submit("C1", "D1", 1100, 1200, "boot-2")
        self.assertEqual(body["segment"]["status"], "unlocatable")
        self.assertIsNone(body["segment"]["utc_start_ms"])
        # 设备重启后 local 从 0 起：boot-2 的 1100 不会借用 default 锚点
        _, body = self.submit("C2", "D1", 1100, 1200, "default")
        self.assertEqual(body["segment"]["status"], "located")

    def test_list_segments_filter_by_cycle(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 2000, 101000, "default")
        self.add_anchor("D1", 1000, 200000, "boot-2")
        self.add_anchor("D1", 2000, 202000, "boot-2")
        self.submit("A", "D1", 1100, 1150, "default")
        self.submit("B", "D1", 1100, 1150, "boot-2")
        _, body = self.req("GET", "/segments?device_id=D1&cycle_id=boot-2")
        self.assertEqual([s["capture_id"] for s in body["segments"]], ["B"])
        # 仅给 cycle_id 不给 device_id -> 400
        status, body = self.req("GET", "/segments?cycle_id=boot-2")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    # ------------------------------------------------------------ 幂等/冲突含周期

    def test_capture_global_unique_cycle_counts_as_content(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        for cyc in ("default", "boot-2"):
            self.add_anchor("D1", 1000, 100000, cyc)
            self.add_anchor("D1", 2000, 101000, cyc)
        # 采集编号全局唯一：同编号提交到不同周期 -> 冲突（周期计入内容）
        self.assertEqual(self.submit("CAP1", "D1", 1100, 1200, "default")[0], 201)
        status, body = self.submit("CAP1", "D1", 1100, 1200, "boot-2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        self.assertEqual(body["error"]["details"]["existing"]["cycle_id"], "default")
        self.assertEqual(body["error"]["details"]["received"]["cycle_id"], "boot-2")
        # 同周期同内容（省略 cycle_id 即 default）重传 -> 幂等
        status, body = self.submit("CAP1", "D1", 1100, 1200)
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        # 仍只有一条
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 1)

    # ------------------------------------------------------------ 时间轴/发布

    def test_timeline_and_publish_show_cycles_and_freeze(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 2000, 101000, "default")
        self.add_anchor("D1", 1000, 200000, "boot-2")
        self.add_anchor("D1", 2000, 202000, "boot-2")
        self.submit("C-DEF", "D1", 1100, 1200, "default")
        self.submit("C-B2", "D1", 1100, 1200, "boot-2")

        # 当前时间轴显示周期归属
        _, tl = self.req("GET", "/timeline/current")
        cyc_ids = {c["cycle_id"] for d in tl["devices"] for c in d["cycles"]}
        self.assertEqual(cyc_ids, {"default", "boot-2"})
        for s in tl["segments"]:
            self.assertIn("cycle_id", s)
        anc_cycles = {a["cycle_id"] for d in tl["devices"] for a in d["anchors"]}
        self.assertEqual(anc_cycles, {"default", "boot-2"})

        # 发布 v1：冻结各周期锚点与声段投影
        status, v1 = self.req("POST", "/versions", {"note": "双周期"})
        self.assertEqual(status, 201)
        self.assertEqual(v1["format_rev"], 2)
        self.assertEqual(len(v1["anchors"]), 4)
        for a in v1["anchors"]:
            self.assertIn("cycle_id", a)
        for s in v1["segments"]:
            self.assertIn("cycle_id", s)

        # 更正 boot-2 锚点（UTC 整体 +500），只影响 boot-2 声段
        status, body = self.req("PUT", "/devices/D1/anchors", {
            "cycle_id": "boot-2",
            "anchors": [{"local_ms": 1000, "utc_ms": 200500},
                        {"local_ms": 2000, "utc_ms": 202500}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_ids"], ["boot-2"])

        _, v2 = self.req("POST", "/versions")
        self.assertEqual(v2["format_rev"], 2)
        # 位移只发生在 boot-2 的 C-B2 上
        displaced = {d["capture_id"]: d for d in v2["changes"]["displaced"]}
        self.assertEqual(set(displaced), {"C-B2"})
        self.assertEqual(displaced["C-B2"]["start_shift_ms"], 500)

        # v1 冻结不变（含各周期锚点）
        _, v1_again = self.req("GET", "/versions/1")
        self.assertEqual(v1_again["anchors"], v1["anchors"])
        self.assertEqual(v1_again["segments"], v1["segments"])

    def test_put_anchors_multi_cycle_and_unknown_rejected(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        # 一次替换多个周期
        status, body = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"cycle_id": "default", "local_ms": 1000, "utc_ms": 100000},
            {"cycle_id": "default", "local_ms": 2000, "utc_ms": 101000},
            {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 200000},
            {"cycle_id": "boot-2", "local_ms": 2000, "utc_ms": 202000},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["cycle_ids"]), ["boot-2", "default"])

        # 引用不存在周期 -> 整体拒绝，不写入（已替换的数据也保留）
        before = self.req("GET", "/devices/D1/anchors")[1]["anchors"]
        status, body = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"cycle_id": "ghost", "local_ms": 1, "utc_ms": 1},
        ]})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        after = self.req("GET", "/devices/D1/anchors")[1]["anchors"]
        self.assertEqual(before, after)

        # 某周期内不递增 -> 整体拒绝
        status, body = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 200500},
            {"cycle_id": "boot-2", "local_ms": 2000, "utc_ms": 200400},
        ]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "anchors_not_increasing")

    def test_delete_anchor_uses_cycle_query(self):
        self.make_device()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 100000, "default")
        self.add_anchor("D1", 1000, 200000, "boot-2")
        # 不带 cycle_id 删 default 的 1000
        status, body = self.req("DELETE", "/devices/D1/anchors/1000")
        self.assertEqual(status, 200)
        self.assertEqual(body["cycle_id"], "default")
        # boot-2 的同 local_ms 锚点仍在
        anchors = self.req("GET", "/devices/D1/anchors")[1]["anchors"]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["cycle_id"], "boot-2")
        # 指定 boot-2 删除
        status, _ = self.req("DELETE", "/devices/D1/anchors/1000?cycle_id=boot-2")
        self.assertEqual(status, 200)
        self.assertEqual(self.req("GET", "/devices/D1/anchors")[1]["anchors"], [])
        # 再删 -> 404
        status, body = self.req("DELETE", "/devices/D1/anchors/1000?cycle_id=boot-2")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "anchor_not_found")


# ------------------------------------------------------------ 旧库迁移

class MigrationTestCase(unittest.TestCase):
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
        db.init_db(self.db_path)  # 在线迁移
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
            # 旧声段内容指纹按旧口径（无周期）
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
            conn.execute("INSERT INTO versions VALUES (1,'旧版','2026-09-02T00:00:00.000Z')")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',1000,100000)")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',2000,101000)")
            conn.execute(
                "INSERT INTO version_segments VALUES (1,'OLD1','D1',1100,1200,NULL,"
                "'located',100100,100200,'t')")
            conn.commit()
        finally:
            conn.close()

    def test_legacy_data_moved_to_default(self):
        # 旧设备多出 default 周期
        _, body = self.req("GET", "/devices/D1/cycles")
        self.assertEqual([c["cycle_id"] for c in body["cycles"]], ["default"])
        # 旧锚点归入 default，且本地毫秒带周期归属
        _, body = self.req("GET", "/devices/D1/anchors")
        self.assertTrue(all(a["cycle_id"] == "default" for a in body["anchors"]))
        # 旧声段归入 default，投影正常
        _, body = self.req("GET", "/segments/OLD1")
        seg = body["segment"]
        self.assertEqual(seg["cycle_id"], "default")
        self.assertEqual(seg["utc_start_ms"], 100100)

    def test_legacy_default_replay_idempotent_after_migration(self):
        # 迁移已按新口径重算指纹：默认周期同内容重传仍幂等
        status, body = self.req("POST", "/segments", {
            "capture_id": "OLD1", "device_id": "D1",
            "local_start_ms": 1100, "local_end_ms": 1200})
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])

    def test_legacy_published_version_unchanged(self):
        # 已发布旧版本：无 cycle_id / format_rev，字段与取值与升级前一致
        status, v1 = self.req("GET", "/versions/1")
        self.assertEqual(status, 200)
        self.assertNotIn("format_rev", v1)
        self.assertEqual(v1["anchors"], [
            {"device_id": "D1", "local_ms": 1000, "utc_ms": 100000},
            {"device_id": "D1", "local_ms": 2000, "utc_ms": 101000},
        ])
        seg = v1["segments"][0]
        self.assertEqual(seg["capture_id"], "OLD1")
        self.assertNotIn("cycle_id", seg)
        self.assertEqual(seg["utc_start_ms"], 100100)
        # changes 里的条目也不带周期字段
        self.assertNotIn("cycle_id", v1["changes"]["added"][0])

    def test_publish_after_migration_is_cycle_format(self):
        status, v2 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["format_rev"], 2)
        self.assertEqual(v2["based_on_version"], 1)
        self.assertTrue(all("cycle_id" in a for a in v2["anchors"]))
        # 与旧版相比声段投影未变 -> 不产生位移
        self.assertEqual(v2["changes"]["displaced"], [])
        self.assertEqual(v2["changes"]["added"], [])
        self.assertEqual(v2["changes"]["removed"], [])

    def test_new_cycle_usable_after_migration(self):
        # 迁移后可正常登记新周期并复用本地毫秒
        status, _ = self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.assertEqual(status, 201)
        status, _ = self.req("POST", "/devices/D1/anchors",
                             {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 200000})
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main(verbosity=2)
