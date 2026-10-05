"""端到端测试：覆盖审计（coverage audit，v1.4）。

覆盖：
- 基本计算：只计可定位声段并与查询半开区间相交；按设备集合变化切成最大
  半开片段，每段返回排序后的设备编号、数量与是否达标；
- 同设备重叠（含相接）声段只算一台；不同设备边界相接不重叠；
- 零长度声段不贡献覆盖；不可定位声段不计；无数据时整段为缺口；
- 不足门槛的相邻片段合并为连续缺口（记录设备并集与峰值设备数）；
- 指定 version_no 的历史审计只依据已冻结声段：后续锚点更正、声段增删、
  周期归属纠正与新发布都不改动历史结果；省略 version_no 审计当前时间轴；
- 输入校验：起点必须小于终点、时间须为 [0, 2^63-1] 整数毫秒、门槛须为
  正整数、版本不存在 -> 明确拒绝且不改动数据；审计本身只读；
- 并发：审计与更正/发布并发时，每次审计响应都是一致快照（片段恰好
  不重不漏地拼满查询区间）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402

MAX_INT64 = 2 ** 63 - 1


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


class CoverageAuditTestCase(unittest.TestCase):
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

    def audit(self, start_ms, end_ms, min_devices, version_no=None):
        payload = {"start_ms": start_ms, "end_ms": end_ms,
                   "min_devices": min_devices}
        if version_no is not None:
            payload["version_no"] = version_no
        return self.req("POST", "/coverage/audit", payload)

    def make_identity_device(self, device_id):
        """建一台恒等映射设备：锚点 (0,0)-(100000,100000)，本地毫秒即 UTC 毫秒。"""
        self.make_device(device_id)
        self.add_anchor(device_id, 0, 0)
        self.add_anchor(device_id, 100000, 100000)

    def assert_partition(self, body, start_ms, end_ms):
        """片段必须不重不漏地恰好拼满查询区间，且长度与统计一致。"""
        fragments = body["fragments"]
        self.assertTrue(fragments)
        self.assertEqual(fragments[0]["start_ms"], start_ms)
        self.assertEqual(fragments[-1]["end_ms"], end_ms)
        for prev, nxt in zip(fragments, fragments[1:]):
            self.assertEqual(prev["end_ms"], nxt["start_ms"])
            self.assertLess(prev["start_ms"], prev["end_ms"])
        self.assertEqual(body["covered_ms"] + body["gap_ms"], end_ms - start_ms)
        covered = sum(f["end_ms"] - f["start_ms"]
                      for f in fragments if f["meets_threshold"])
        self.assertEqual(body["covered_ms"], covered)
        # 缺口同样不重不漏地拼满所有不达标片段
        gap_ms = sum(g["end_ms"] - g["start_ms"] for g in body["gaps"])
        self.assertEqual(body["gap_ms"], gap_ms)
        self.assertEqual(body["ok"], not body["gaps"])

    # ------------------------------------------------------------ 基本计算

    def test_basic_fragments_threshold_and_gaps(self):
        """两台设备部分重叠：按设备集合变化切三段，两端不达标各成一个缺口。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D2", 5000, 15000)

        status, body = self.audit(0, 15000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "current")
        self.assertIsNone(body["version_no"])
        self.assertEqual(body["located_segment_count"], 2)
        self.assertEqual(body["fragments"], [
            {"start_ms": 0, "end_ms": 5000, "device_ids": ["D1"],
             "device_count": 1, "meets_threshold": False},
            {"start_ms": 5000, "end_ms": 10000, "device_ids": ["D1", "D2"],
             "device_count": 2, "meets_threshold": True},
            {"start_ms": 10000, "end_ms": 15000, "device_ids": ["D2"],
             "device_count": 1, "meets_threshold": False},
        ])
        # 两个缺口被达标片段隔开，不合并
        self.assertEqual(body["gaps"], [
            {"start_ms": 0, "end_ms": 5000, "device_ids": ["D1"],
             "max_device_count": 1},
            {"start_ms": 10000, "end_ms": 15000, "device_ids": ["D2"],
             "max_device_count": 1},
        ])
        self.assertFalse(body["ok"])
        self.assertEqual(body["covered_ms"], 5000)
        self.assertEqual(body["gap_ms"], 10000)
        self.assert_partition(body, 0, 15000)

    def test_same_device_overlap_counts_once(self):
        """同设备的重叠声段只算一台：整段设备数为 1，合并为一个缺口。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D1", 5000, 15000)

        status, body = self.audit(0, 15000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["located_segment_count"], 2)
        self.assertEqual(body["fragments"], [
            {"start_ms": 0, "end_ms": 15000, "device_ids": ["D1"],
             "device_count": 1, "meets_threshold": False},
        ])
        self.assertEqual(body["gaps"], [
            {"start_ms": 0, "end_ms": 15000, "device_ids": ["D1"],
             "max_device_count": 1},
        ])
        self.assert_partition(body, 0, 15000)

    def test_touching_boundaries_not_overlapping(self):
        """边界相接不重叠：D1 [0,5000) 与 D2 [5000,10000) 在 5000 处换班，
        任一时刻都只有一台设备；相邻不达标片段合并为一个缺口。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 5000)
        self.submit("CAP-2", "D2", 5000, 10000)

        status, body = self.audit(0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [
            {"start_ms": 0, "end_ms": 5000, "device_ids": ["D1"],
             "device_count": 1, "meets_threshold": False},
            {"start_ms": 5000, "end_ms": 10000, "device_ids": ["D2"],
             "device_count": 1, "meets_threshold": False},
        ])
        # 相邻缺口合并：设备并集 + 峰值设备数
        self.assertEqual(body["gaps"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": ["D1", "D2"],
             "max_device_count": 1},
        ])
        self.assert_partition(body, 0, 10000)

    def test_touching_same_device_is_continuous(self):
        """同设备首尾相接的两段：该设备持续在线，不切分、不产生缺口。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 5000)
        self.submit("CAP-2", "D1", 5000, 10000)

        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": ["D1"],
             "device_count": 1, "meets_threshold": True},
        ])
        self.assertEqual(body["gaps"], [])
        self.assertTrue(body["ok"])
        self.assert_partition(body, 0, 10000)

    def test_zero_length_segment_contributes_nothing(self):
        """零长度声段（起点=终点，可定位）不贡献覆盖，也不切分片段。"""
        self.make_identity_device("D1")
        self.submit("CAP-ZERO", "D1", 5000, 5000)

        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["located_segment_count"], 0)
        self.assertEqual(body["fragments"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": [],
             "device_count": 0, "meets_threshold": False},
        ])
        self.assertEqual(len(body["gaps"]), 1)

        # 零长度声段落在正常声段内部：不产生额外切分
        self.submit("CAP-1", "D1", 0, 10000)
        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": ["D1"],
             "device_count": 1, "meets_threshold": True},
        ])
        self.assertTrue(body["ok"])

    def test_unlocatable_segments_ignored(self):
        """不可定位声段（超出锚点覆盖范围）不计入覆盖。"""
        self.make_identity_device("D1")
        self.submit("CAP-OUT", "D1", 200000, 300000)  # 超出锚点覆盖
        _, seg = self.req("GET", "/segments/CAP-OUT")
        self.assertEqual(seg["segment"]["status"], "unlocatable")

        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["located_segment_count"], 0)
        self.assertEqual(body["fragments"][0]["device_ids"], [])
        self.assertEqual(body["gaps"], [
            {"start_ms": 0, "end_ms": 10000, "device_ids": [],
             "max_device_count": 0},
        ])

    def test_no_data_whole_interval_is_gap(self):
        """无数据时整段为缺口。"""
        status, body = self.audit(1000, 2000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["located_segment_count"], 0)
        self.assertEqual(body["fragments"], [
            {"start_ms": 1000, "end_ms": 2000, "device_ids": [],
             "device_count": 0, "meets_threshold": False},
        ])
        self.assertEqual(body["gaps"], [
            {"start_ms": 1000, "end_ms": 2000, "device_ids": [],
             "max_device_count": 0},
        ])
        self.assertFalse(body["ok"])
        self.assertEqual(body["covered_ms"], 0)
        self.assertEqual(body["gap_ms"], 1000)

    def test_clipping_to_query_interval(self):
        """声段与查询区间相交：只计相交部分，片段边界被裁剪到查询区间。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)

        status, body = self.audit(3000, 6000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [
            {"start_ms": 3000, "end_ms": 6000, "device_ids": ["D1"],
             "device_count": 1, "meets_threshold": True},
        ])
        self.assertTrue(body["ok"])
        self.assertEqual(body["covered_ms"], 3000)

        # 查询区间只与声段边界相接（[10000,20000) 与 [0,10000)）-> 无覆盖
        status, body = self.audit(10000, 20000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["located_segment_count"], 0)
        self.assertEqual(body["fragments"][0]["device_ids"], [])

    def test_all_met_single_fragment_sorted_device_ids(self):
        """整段达标：单个最大片段；设备编号排序输出（与注册顺序无关）。"""
        self.make_identity_device("D3")
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D3", 0, 10000)
        self.submit("CAP-2", "D1", 0, 10000)
        self.submit("CAP-3", "D2", 0, 10000)

        status, body = self.audit(0, 10000, 3)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [
            {"start_ms": 0, "end_ms": 10000,
             "device_ids": ["D1", "D2", "D3"],
             "device_count": 3, "meets_threshold": True},
        ])
        self.assertEqual(body["gaps"], [])
        self.assertTrue(body["ok"])
        self.assertEqual(body["covered_ms"], 10000)
        self.assertEqual(body["gap_ms"], 0)

    def test_int64_boundary(self):
        """时间上限 2^63-1：大数值区间可正常审计。"""
        status, body = self.audit(0, MAX_INT64, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["gap_ms"], MAX_INT64)

        self.make_device("D1")
        self.add_anchor("D1", MAX_INT64 - 2, MAX_INT64 - 2)
        self.add_anchor("D1", MAX_INT64 - 1, MAX_INT64 - 1)
        self.submit("CAP-EDGE", "D1", MAX_INT64 - 2, MAX_INT64 - 1)
        status, body = self.audit(MAX_INT64 - 2, MAX_INT64 - 1, 1)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["covered_ms"], 1)

    # ------------------------------------------------------------ 版本审计

    def test_version_audit_frozen_history(self):
        """历史审计只依据已冻结声段：发布后的锚点更正、声段增删、
        周期归属纠正与新发布都不改动历史结果；当前视图则反映最新状态。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D2", 5000, 15000)
        status, _ = self.req("POST", "/versions", {"note": "v1"})
        self.assertEqual(status, 201)

        status, before = self.audit(0, 15000, 2, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(before["source"], "version")
        self.assertEqual(before["version_no"], 1)
        self.assertEqual(len(before["fragments"]), 3)

        # 发布后：锚点更正（D1 投影位移）、删除声段、新增声段、周期归属纠正
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 250},
                              {"local_ms": 100000, "utc_ms": 100250}]})
        self.req("DELETE", "/segments/CAP-2")
        self.submit("CAP-3", "D2", 20000, 30000)
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 0, "utc_ms": 500000})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 100000, "utc_ms": 600000})
        status, _ = self.req("POST", "/segments/reassign-cycle",
                             {"items": [{"capture_id": "CAP-1",
                                         "from_cycle_id": "default",
                                         "to_cycle_id": "boot-2"}]})
        self.assertEqual(status, 200)
        self.req("POST", "/versions", {"note": "v2"})

        # 历史版本审计结果逐字段不变
        status, after = self.audit(0, 15000, 2, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(after, before)

        # 当前视图反映最新状态：CAP-1 移到 boot-2（映射到 500250 起），
        # CAP-2 已删除，[0,15000) 内无任何可定位声段 -> 整段缺口
        status, current = self.audit(0, 15000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(current["source"], "current")
        self.assertEqual(current["located_segment_count"], 0)
        self.assertEqual(current["gaps"], [
            {"start_ms": 0, "end_ms": 15000, "device_ids": [],
             "max_device_count": 0},
        ])

        # v2 的审计与当前时间轴一致（v2 即当前状态的冻结）
        status, v2 = self.audit(0, 15000, 2, version_no=2)
        self.assertEqual(status, 200)
        self.assertEqual(v2["fragments"], current["fragments"])
        self.assertEqual(v2["gaps"], current["gaps"])

    def test_version_audit_uses_frozen_projection_not_current_anchors(self):
        """版本审计使用发布时冻结的 UTC 投影，而不是按当前锚点重算。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        # 更正锚点使投影整体平移 +5000：当前视图下 [0,10000) 不再有覆盖
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 5000},
                              {"local_ms": 100000, "utc_ms": 105000}]})

        status, cur = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertFalse(cur["ok"])  # 当前视图：声段已移到 [5000,15000)
        self.assertEqual(cur["gap_ms"], 5000)

        status, v1 = self.audit(0, 10000, 1, version_no=1)
        self.assertEqual(status, 200)
        self.assertTrue(v1["ok"])  # 历史视图：发布时投影为 [0,10000)
        self.assertEqual(v1["covered_ms"], 10000)

    # ------------------------------------------------------------ 输入校验

    def test_invalid_inputs_rejected_and_data_unchanged(self):
        """无效输入或版本不存在：明确拒绝且不改动任何数据。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        cases = [
            # 起点必须小于终点
            ({"start_ms": 10000, "end_ms": 10000, "min_devices": 1}, 400, "invalid_range"),
            ({"start_ms": 20000, "end_ms": 10000, "min_devices": 1}, 400, "invalid_range"),
            # 时间须为 [0, 2^63-1] 的整数毫秒
            ({"start_ms": -1, "end_ms": 10000, "min_devices": 1}, 400, "invalid_time"),
            ({"start_ms": 0, "end_ms": MAX_INT64 + 1, "min_devices": 1}, 400, "invalid_time"),
            ({"start_ms": 0.5, "end_ms": 10000, "min_devices": 1}, 400, "invalid_time"),
            ({"start_ms": "0", "end_ms": 10000, "min_devices": 1}, 400, "invalid_time"),
            ({"start_ms": True, "end_ms": 10000, "min_devices": 1}, 400, "invalid_time"),
            ({"start_ms": None, "end_ms": 10000, "min_devices": 1}, 400, "invalid_time"),
            # 门槛须为正整数
            ({"start_ms": 0, "end_ms": 10000, "min_devices": 0}, 400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": -2}, 400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": 1.5}, 400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": "2"}, 400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": True}, 400, "invalid_threshold"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": None}, 400, "invalid_threshold"),
            # 缺字段 / 版本号非法 / 版本不存在
            ({"start_ms": 0, "end_ms": 10000}, 400, "validation_error"),
            ({"end_ms": 10000, "min_devices": 1}, 400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": 1, "version_no": 0},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": 1, "version_no": -1},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": 1, "version_no": "1"},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": 1, "version_no": 1.0},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "min_devices": 1, "version_no": 999},
             404, "version_not_found"),
        ]
        for payload, want_status, want_code in cases:
            with self.subTest(payload=payload):
                status, body = self.req("POST", "/coverage/audit", payload)
                self.assertEqual(status, want_status)
                self.assertEqual(body["error"]["code"], want_code)

        # 请求体非对象 / 空请求体
        status, body = self.req("POST", "/coverage/audit", [1, 2, 3])
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))
        status, body = self.req("POST", "/coverage/audit")
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))

        # 全部拒绝后数据不变
        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_timeline["devices"], before_timeline["devices"])
        self.assertEqual(after_versions, before_versions)

    def test_audit_is_read_only(self):
        """合法审计同样是只读的：不改动时间轴与版本数据。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        status, _ = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        status, _ = self.audit(0, 10000, 1, version_no=1)
        self.assertEqual(status, 200)

        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_versions, before_versions)

    # ------------------------------------------------------------ 并发一致性

    def test_concurrent_audit_with_writes(self):
        """审计与锚点更正/发布并发：每次审计响应都必须是一致快照——
        片段不重不漏地恰好拼满查询区间，统计自洽。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        for i in range(5):
            self.submit(f"CAP-{i}", "D1", i * 2000, i * 2000 + 1500)
            self.submit(f"CAP-B{i}", "D2", i * 2000 + 500, i * 2000 + 2000)

        errors, audits = [], []

        def correct(k):
            try:
                self.req("PUT", "/devices/D1/anchors",
                         {"anchors": [{"local_ms": 0, "utc_ms": k * 10},
                                      {"local_ms": 100000, "utc_ms": 100000 + k * 10}]})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                self.req("POST", "/versions")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def audit():
            try:
                status, body = self.audit(0, 12000, 2)
                if status != 200:
                    errors.append(("audit_status", status, body))
                else:
                    audits.append(body)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(1, 6):
            threads.append(threading.Thread(target=correct, args=(k,)))
            threads.append(threading.Thread(target=publish))
            threads.append(threading.Thread(target=audit))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(audits)
        for body in audits:
            self.assert_partition(body, 0, 12000)


if __name__ == "__main__":
    unittest.main()
