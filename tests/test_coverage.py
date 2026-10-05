"""端到端测试：覆盖审计（coverage audit，v1.4）。

覆盖：
- 基本计算：只计可定位声段并与查询半开区间相交；按设备集合变化切成最大
  半开片段；每段返回排序后的设备编号、数量与是否达标；
- 同设备重叠只算一台；边界相接不重叠；同设备相接片段合并为最大片段；
  零长度声段不贡献覆盖；不可定位声段不计；窗口外声段不计、边界裁剪；
- 不足门槛的相邻缺口合并；无数据时整段为缺口；
- 输入校验：非法时间 / 起点不小于终点 / 门槛非正整数 / 版本号非法或不存在，
  均明确拒绝且不改变数据（本接口只读）；
- 历史审计只依据已冻结声段：后续锚点更正、周期归属纠正、声段增删不影响
  已发布版本的审计结果；当前视图取一致快照（并发下审计/更正/发布互不干裂）。

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


def frag(start, end, devices, min_devices):
    return {"start_ms": start, "end_ms": end, "devices": devices,
            "device_count": len(devices),
            "meets_threshold": len(devices) >= min_devices}


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

    def make_device(self, device_id="D1", offset=1000):
        """注册设备并录入斜率为 1 的锚点：utc = local + offset（local ∈ [0,100000]）。"""
        self.req("POST", "/devices", {"device_id": device_id})
        self.req("POST", f"/devices/{device_id}/anchors",
                 {"local_ms": 0, "utc_ms": offset})
        self.req("POST", f"/devices/{device_id}/anchors",
                 {"local_ms": 100000, "utc_ms": 100000 + offset})

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

    def make_two_device_scene(self):
        """D1: utc[2000,6000)；D2: utc[3000,5000)（两设备锚点均为 utc=local+1000）。"""
        self.make_device("D1", offset=1000)
        self.make_device("D2", offset=1000)
        self.submit("CAP-1", "D1", 1000, 5000)   # utc [2000, 6000)
        self.submit("CAP-2", "D2", 2000, 4000)   # utc [3000, 5000)

    # ------------------------------------------------------------ 基本计算

    def test_basic_fragments_and_gaps(self):
        self.make_two_device_scene()
        status, body = self.audit(0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "current")
        self.assertIsNone(body["version_no"])
        self.assertEqual(body["start_ms"], 0)
        self.assertEqual(body["end_ms"], 10000)
        self.assertEqual(body["min_devices"], 2)
        self.assertFalse(body["fully_covered"])
        self.assertEqual(body["fragments"], [
            frag(0, 2000, [], 2),
            frag(2000, 3000, ["D1"], 2),
            frag(3000, 5000, ["D1", "D2"], 2),
            frag(5000, 6000, ["D1"], 2),
            frag(6000, 10000, [], 2),
        ])
        # 不足门槛的相邻片段合并为连续缺口
        self.assertEqual(body["gaps"], [
            {"start_ms": 0, "end_ms": 3000, "max_device_count": 1},
            {"start_ms": 5000, "end_ms": 10000, "max_device_count": 1},
        ])

    def test_fully_covered_when_threshold_met(self):
        self.make_two_device_scene()
        # 窗口完全落在双设备重叠区
        status, body = self.audit(3000, 5000, 2)
        self.assertEqual(status, 200)
        self.assertTrue(body["fully_covered"])
        self.assertEqual(body["fragments"], [frag(3000, 5000, ["D1", "D2"], 2)])
        self.assertEqual(body["gaps"], [])
        # 门槛降为 1：三段都达标（设备集合不同仍是不同片段）
        status, body = self.audit(2000, 6000, 1)
        self.assertEqual(status, 200)
        self.assertTrue(body["fully_covered"])
        self.assertEqual(body["fragments"], [
            frag(2000, 3000, ["D1"], 1),
            frag(3000, 5000, ["D1", "D2"], 1),
            frag(5000, 6000, ["D1"], 1),
        ])
        self.assertEqual(body["gaps"], [])

    def test_same_device_overlap_counts_once(self):
        """同设备的重叠声段只算一台。"""
        self.make_device("D1", offset=1000)
        self.submit("CAP-1", "D1", 1000, 5000)   # utc [2000, 6000)
        self.submit("CAP-2", "D1", 3000, 7000)   # utc [4000, 8000)，与 CAP-1 重叠
        status, body = self.audit(0, 10000, 2)
        self.assertEqual(status, 200)
        # 重叠区 [4000,6000) 仍只有 D1 一台；设备集合不变 -> 合并为最大片段
        self.assertEqual(body["fragments"], [
            frag(0, 2000, [], 2),
            frag(2000, 8000, ["D1"], 2),
            frag(8000, 10000, [], 2),
        ])
        self.assertEqual(body["gaps"],
                         [{"start_ms": 0, "end_ms": 10000, "max_device_count": 1}])
        # 门槛 1：中段达标
        status, body = self.audit(0, 10000, 1)
        self.assertEqual([f["meets_threshold"] for f in body["fragments"]],
                         [False, True, False])
        self.assertEqual(body["gaps"], [
            {"start_ms": 0, "end_ms": 2000, "max_device_count": 0},
            {"start_ms": 8000, "end_ms": 10000, "max_device_count": 0},
        ])

    def test_touching_boundaries_not_overlapping(self):
        """不同设备的声段边界相接（end == start）不算重叠。"""
        self.make_device("D1", offset=1000)
        self.make_device("D2", offset=1000)
        self.submit("CAP-1", "D1", 1000, 4000)   # utc [2000, 5000)
        self.submit("CAP-2", "D2", 4000, 7000)   # utc [5000, 8000)，与 CAP-1 相接
        status, body = self.audit(0, 10000, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [
            frag(0, 2000, [], 2),
            frag(2000, 5000, ["D1"], 2),
            frag(5000, 8000, ["D2"], 2),
            frag(8000, 10000, [], 2),
        ])
        # 任何时刻都不满 2 台 -> 整段一个缺口
        self.assertEqual(body["gaps"],
                         [{"start_ms": 0, "end_ms": 10000, "max_device_count": 1}])

    def test_same_device_touching_segments_merge(self):
        """同设备相接声段设备集合不变 -> 合并为最大片段。"""
        self.make_device("D1", offset=1000)
        self.submit("CAP-1", "D1", 1000, 4000)   # utc [2000, 5000)
        self.submit("CAP-2", "D1", 4000, 7000)   # utc [5000, 8000)
        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [
            frag(0, 2000, [], 1),
            frag(2000, 8000, ["D1"], 1),   # 相接处合并
            frag(8000, 10000, [], 1),
        ])

    def test_zero_length_segment_contributes_nothing(self):
        """零长度声段（utc_start == utc_end）不贡献覆盖。"""
        self.make_device("D1", offset=1000)
        self.make_device("D2", offset=1000)
        self.submit("CAP-Z", "D1", 1500, 1500)   # 可定位但零长度：utc [2500, 2500)
        status, seg = self.req("GET", "/segments/CAP-Z")
        self.assertEqual(seg["segment"]["status"], "located")
        self.assertEqual(seg["segment"]["utc_start_ms"],
                         seg["segment"]["utc_end_ms"])
        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [frag(0, 10000, [], 1)])
        self.assertEqual(body["gaps"],
                         [{"start_ms": 0, "end_ms": 10000, "max_device_count": 0}])
        # 零长度声段落在其他设备覆盖内也不改变设备集合
        self.submit("CAP-2", "D2", 1000, 5000)   # utc [2000, 6000)
        status, body = self.audit(0, 10000, 1)
        self.assertEqual(body["fragments"], [
            frag(0, 2000, [], 1),
            frag(2000, 6000, ["D2"], 1),
            frag(6000, 10000, [], 1),
        ])

    def test_unlocatable_segments_ignored(self):
        """只计可定位声段：不可定位声段不参与覆盖。"""
        self.make_device("D1", offset=1000)
        self.submit("CAP-OUT", "D1", 200000, 201000)  # 越出锚点覆盖 -> 不可定位
        status, seg = self.req("GET", "/segments/CAP-OUT")
        self.assertEqual(seg["segment"]["status"], "unlocatable")
        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["fragments"], [frag(0, 10000, [], 1)])
        self.assertEqual(body["gaps"],
                         [{"start_ms": 0, "end_ms": 10000, "max_device_count": 0}])

    def test_segments_clipped_to_query_window(self):
        """声段与查询区间相交：窗口外部分裁剪，完全在外（含边界相接）不计。"""
        self.make_device("D1", offset=1000)
        self.submit("CAP-1", "D1", 1000, 5000)   # utc [2000, 6000)
        # 窗口落在声段内部 -> 整段达标
        status, body = self.audit(3000, 5000, 1)
        self.assertEqual(body["fragments"], [frag(3000, 5000, ["D1"], 1)])
        self.assertTrue(body["fully_covered"])
        # 两侧裁剪
        status, body = self.audit(1500, 6500, 1)
        self.assertEqual(body["fragments"], [
            frag(1500, 2000, [], 1),
            frag(2000, 6000, ["D1"], 1),
            frag(6000, 6500, [], 1),
        ])
        # 声段终点恰为窗口起点（半开，不相交）-> 不贡献
        status, body = self.audit(6000, 9000, 1)
        self.assertEqual(body["fragments"], [frag(6000, 9000, [], 1)])
        # 声段起点恰为窗口终点 -> 不贡献
        status, body = self.audit(0, 2000, 1)
        self.assertEqual(body["fragments"], [frag(0, 2000, [], 1)])

    def test_no_data_whole_interval_is_gap(self):
        """无数据时整段为缺口。"""
        status, body = self.audit(0, 10000, 1)
        self.assertEqual(status, 200)
        self.assertFalse(body["fully_covered"])
        self.assertEqual(body["fragments"], [frag(0, 10000, [], 1)])
        self.assertEqual(body["gaps"],
                         [{"start_ms": 0, "end_ms": 10000, "max_device_count": 0}])
        # 有设备有锚点但没有任何声段 -> 同样整段缺口
        self.make_device("D1", offset=1000)
        status, body = self.audit(0, 10000, 3)
        self.assertEqual(body["fragments"], [frag(0, 10000, [], 3)])
        self.assertEqual(body["gaps"],
                         [{"start_ms": 0, "end_ms": 10000, "max_device_count": 0}])

    def test_adjacent_gaps_merged(self):
        """多个低于门槛的相邻片段（设备集合不同）合并为一个连续缺口。"""
        self.make_device("D1", offset=1000)
        self.make_device("D2", offset=1000)
        self.submit("CAP-1", "D1", 1000, 2000)   # utc [2000, 3000)
        self.submit("CAP-2", "D2", 3000, 4000)   # utc [4000, 5000)
        status, body = self.audit(0, 10000, 2)
        self.assertEqual(status, 200)
        # 片段仍按设备集合变化切分
        self.assertEqual(body["fragments"], [
            frag(0, 2000, [], 2),
            frag(2000, 3000, ["D1"], 2),
            frag(3000, 4000, [], 2),
            frag(4000, 5000, ["D2"], 2),
            frag(5000, 10000, [], 2),
        ])
        # 缺口合并为一个连续区间，记录期间最大设备数
        self.assertEqual(body["gaps"],
                         [{"start_ms": 0, "end_ms": 10000, "max_device_count": 1}])

    # ------------------------------------------------------------ 输入校验

    def test_invalid_time_rejected(self):
        self.make_two_device_scene()
        for field in ("start_ms", "end_ms"):
            for bad in (-1, MAX_INT64 + 1, 1.5, "100", True, None):
                payload = {"start_ms": 0, "end_ms": 10000, "min_devices": 1,
                           field: bad}
                status, body = self.req("POST", "/coverage/audit", payload)
                self.assertEqual(status, 400, (field, bad))
                self.assertEqual(body["error"]["code"], "invalid_time", (field, bad))
        # 边界值本身合法
        status, _ = self.audit(0, MAX_INT64, 1)
        self.assertEqual(status, 200)
        # 缺字段 / 请求体非法
        for bad in ({}, {"start_ms": 0}, {"end_ms": 1, "min_devices": 1}):
            status, body = self.req("POST", "/coverage/audit", bad)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
        status, body = self.req("POST", "/coverage/audit", [1, 2])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_range_and_threshold_rejected(self):
        self.make_two_device_scene()
        # 起点必须小于终点
        for s, e in ((5000, 5000), (6000, 5000)):
            status, body = self.audit(s, e, 1)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "invalid_range")
        # 门槛必须为正整数
        for bad in (0, -3, 1.5, "2", True, None):
            status, body = self.audit(0, 10000, bad)
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "validation_error", bad)

    def test_version_param_validation(self):
        self.make_two_device_scene()
        status, _ = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        # 版本号类型非法 -> 400
        for bad in ("1", 1.5, True):
            status, body = self.req("POST", "/coverage/audit",
                                    {"start_ms": 0, "end_ms": 10000,
                                     "min_devices": 1, "version_no": bad})
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "validation_error", bad)
        # 版本不存在（含 < 1）-> 404
        for bad in (0, -1, 999):
            status, body = self.req("POST", "/coverage/audit",
                                    {"start_ms": 0, "end_ms": 10000,
                                     "min_devices": 1, "version_no": bad})
            self.assertEqual(status, 404, bad)
            self.assertEqual(body["error"]["code"], "version_not_found", bad)
        # 合法版本号正常审计
        status, body = self.audit(0, 10000, 2, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "version")
        self.assertEqual(body["version_no"], 1)

    # ------------------------------------------------------------ 只读与历史冻结

    def test_audit_is_readonly(self):
        """审计（含被拒绝的审计）不改变任何数据。"""
        self.make_two_device_scene()
        self.req("POST", "/versions")
        before_timeline = self.req("GET", "/timeline/current")[1]
        before_timeline.pop("generated_at")  # 时间戳除外
        before_versions = self.req("GET", "/versions")[1]
        before_v1 = self.req("GET", "/versions/1")[1]
        before_segments = self.req("GET", "/segments")[1]
        # 一批合法与非法审计
        self.audit(0, 10000, 2)
        self.audit(0, 10000, 2, version_no=1)
        self.audit(5000, 5000, 1)
        self.audit(0, 10000, 0)
        self.audit(0, 10000, 1, version_no=999)
        self.req("POST", "/coverage/audit", {"start_ms": "x"})
        after_timeline = self.req("GET", "/timeline/current")[1]
        after_timeline.pop("generated_at")
        self.assertEqual(after_timeline, before_timeline)
        self.assertEqual(self.req("GET", "/versions")[1], before_versions)
        self.assertEqual(self.req("GET", "/versions/1")[1], before_v1)
        self.assertEqual(self.req("GET", "/segments")[1], before_segments)

    def test_version_audit_uses_frozen_segments(self):
        """历史审计只依据已冻结声段：后续锚点/周期变更不改动历史结果。"""
        self.make_device("D1", offset=1000)          # default: utc = local + 1000
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 0, "utc_ms": 9000})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 100000, "utc_ms": 109000})
        self.submit("CAP-1", "D1", 1000, 5000)       # default: utc [2000, 6000)
        status, v1 = self.req("POST", "/versions")
        self.assertEqual(status, 201)

        status, before = self.audit(0, 20000, 1, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(before["fragments"], [
            frag(0, 2000, [], 1),
            frag(2000, 6000, ["D1"], 1),
            frag(6000, 20000, [], 1),
        ])

        # 后续变更：锚点更正（default 平移）、周期归属纠正（CAP-1 -> boot-2）、
        # 新增声段——当前时间轴随之改变
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 2000},
                              {"local_ms": 100000, "utc_ms": 102000}]})
        status, _ = self.req("POST", "/segments/reassign-cycle",
                             {"items": [{"capture_id": "CAP-1",
                                         "from_cycle_id": "default",
                                         "to_cycle_id": "boot-2"}]})
        self.assertEqual(status, 200)                # CAP-1 -> utc [10000, 14000)
        self.submit("CAP-2", "D1", 1000, 5000)       # default 新锚点: utc [3000, 7000)

        status, current = self.audit(0, 20000, 1)
        self.assertEqual(status, 200)
        self.assertEqual(current["scope"], "current")
        self.assertEqual(current["fragments"], [
            frag(0, 3000, [], 1),
            frag(3000, 7000, ["D1"], 1),
            frag(7000, 10000, [], 1),
            frag(10000, 14000, ["D1"], 1),
            frag(14000, 20000, [], 1),
        ])

        # 已发布版本的审计结果逐字节不变（不受锚点更正/周期纠正/新增声段影响）
        status, after = self.audit(0, 20000, 1, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(after, before)
        # 历史版本按当时投影判定门槛
        status, body = self.audit(0, 20000, 2, version_no=1)
        self.assertEqual(body["fragments"], [
            frag(0, 2000, [], 2),
            frag(2000, 6000, ["D1"], 2),
            frag(6000, 20000, [], 2),
        ])

    def test_concurrent_audits_and_writes(self):
        """审计与更正/发布并发：当前视图快照一致，历史版本结果稳定。"""
        self.make_device("D1", offset=1000)
        self.make_device("D2", offset=1000)
        for i in range(4):
            self.submit(f"CAP-A{i}", "D1", 1000 + i * 500, 4000 + i * 500)
            self.submit(f"CAP-B{i}", "D2", 2000 + i * 500, 5000 + i * 500)
        status, _ = self.req("POST", "/versions")
        self.assertEqual(status, 201)

        results, errors = [], []

        def audit_current():
            try:
                results.append(self.audit(0, 20000, 2)[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def audit_v1():
            try:
                results.append(self.audit(0, 20000, 2, version_no=1)[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def correct(k):
            try:
                results.append(self.req("PUT", "/devices/D1/anchors", {
                    "anchors": [{"local_ms": 0, "utc_ms": 1000 + k * 10},
                                {"local_ms": 100000, "utc_ms": 101000 + k * 10}]})[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                results.append(self.req("POST", "/versions")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(5):
            threads += [threading.Thread(target=audit_current),
                        threading.Thread(target=audit_v1),
                        threading.Thread(target=correct, args=(k,)),
                        threading.Thread(target=publish)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(s in (200, 201) for s in results))
        # 历史版本审计结果稳定：无论何时执行都一致
        status, ref = self.audit(0, 20000, 2, version_no=1)
        self.assertEqual(status, 200)
        for _ in range(3):
            self.assertEqual(self.audit(0, 20000, 2, version_no=1)[1], ref)
        # 版本号连续无空洞
        _, body = self.req("GET", "/versions")
        self.assertEqual([v["version_no"] for v in body["versions"]],
                         list(range(1, len(body["versions"]) + 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
