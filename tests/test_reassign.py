"""端到端测试：周期归属批量纠正（reassign，v1.3）。

覆盖：
- 基本成功：声段在同一设备的已登记周期之间移动，起止本地毫秒、备注、
  创建时间与采集编号保持不变，返回按目标周期锚点计算的当前定位结果
  （目标周期覆盖不足仍为 unlocatable）；
- 整批拒绝且不改动任何数据：编号不存在 / 同批重复 / 原周期与预期不符 /
  目标周期未登记（含跨设备同名周期）/ 源或目标周期已封存 /
  原周期等于目标周期 / 请求体非法；
- 相关周期（源+目标）内容指纹随之更新，无关周期指纹不变；
- 纠正后同编号重传：新周期同内容幂等、旧周期或异内容 409 capture_conflict；
- 已发布版本保持原快照，之后发布的版本反映新归属（changes 体现位移）；
- 与锚点更正、版本发布并发时每个版本仍是完整自洽的快照（单写锁串行）。

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


class ReassignApiTestCase(unittest.TestCase):
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

    def reassign(self, items):
        return self.req("POST", "/segments/reassign-cycle", {"items": items})

    def get_segment(self, capture_id):
        return self.req("GET", f"/segments/{capture_id}")

    def fingerprint(self, device_id, cycle_id):
        status, body = self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")
        self.assertEqual(status, 200)
        return body["cycle"]["fingerprint"]

    def seal(self, device_id, cycle_id):
        fp = self.fingerprint(device_id, cycle_id)
        status, body = self.req("POST", f"/devices/{device_id}/cycles/{cycle_id}/seal",
                                {"fingerprint": fp})
        self.assertEqual(status, 201)
        return body

    def make_two_cycle_device(self, device_id="D1"):
        """建一台带 default / boot-2 双周期的设备，两周期各两只锚点。

        default: (1000,100000)-(2000,101000)；boot-2: (1000,500000)-(2000,501000)。
        相同 local 起止在两周期映射到不同 UTC，便于观察归属变化。
        """
        self.make_device(device_id)
        self.reg_cycle(device_id, "boot-2", "第二次开机")
        self.add_anchor(device_id, 1000, 100000, "default")
        self.add_anchor(device_id, 2000, 101000, "default")
        self.add_anchor(device_id, 1000, 500000, "boot-2")
        self.add_anchor(device_id, 2000, 501000, "boot-2")

    # ------------------------------------------------------------ 成功场景

    def test_reassign_single_segment_success(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default", note="鸟鸣")
        before = self.get_segment("CAP-1")[1]["segment"]
        self.assertEqual(before["cycle_id"], "default")
        self.assertEqual(before["utc_start_ms"], 100100)

        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        self.assertEqual(body["moved"], 1)
        (seg,) = body["segments"]
        # 按目标周期锚点计算的当前定位结果
        self.assertEqual(seg["capture_id"], "CAP-1")
        self.assertEqual(seg["cycle_id"], "boot-2")
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["utc_start_ms"], 500100)
        self.assertEqual(seg["utc_end_ms"], 500200)
        # 起止本地毫秒、备注、创建时间与采集编号保持不变
        self.assertEqual(seg["local_start_ms"], 1100)
        self.assertEqual(seg["local_end_ms"], 1200)
        self.assertEqual(seg["note"], "鸟鸣")
        self.assertEqual(seg["created_at"], before["created_at"])
        # 单个查询同样反映新归属
        after = self.get_segment("CAP-1")[1]["segment"]
        self.assertEqual(after["cycle_id"], "boot-2")
        self.assertEqual(after["utc_start_ms"], 500100)
        self.assertEqual(after["created_at"], before["created_at"])

    def test_reassign_batch_across_devices(self):
        """一批可含多台设备的声段：每项在各自设备的已登记周期之间移动。"""
        self.make_two_cycle_device("D1")
        self.make_two_cycle_device("D2")
        self.submit("CAP-A", "D1", 1100, 1200, "default")
        self.submit("CAP-B", "D2", 1100, 1200, "boot-2")
        status, body = self.reassign([
            {"capture_id": "CAP-A", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-B", "from_cycle_id": "boot-2", "to_cycle_id": "default"}])
        self.assertEqual(status, 200)
        self.assertEqual(body["moved"], 2)
        by_id = {s["capture_id"]: s for s in body["segments"]}
        self.assertEqual(by_id["CAP-A"]["cycle_id"], "boot-2")
        self.assertEqual(by_id["CAP-A"]["device_id"], "D1")
        self.assertEqual(by_id["CAP-A"]["utc_start_ms"], 500100)
        self.assertEqual(by_id["CAP-B"]["cycle_id"], "default")
        self.assertEqual(by_id["CAP-B"]["device_id"], "D2")
        self.assertEqual(by_id["CAP-B"]["utc_start_ms"], 100100)

    def test_reassign_swap_two_segments(self):
        """两段互换周期（目标周期恰为另一项的源周期）也可一批完成。"""
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        self.submit("CAP-2", "D1", 1300, 1400, "boot-2")
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-2", "from_cycle_id": "boot-2", "to_cycle_id": "default"}])
        self.assertEqual(status, 200)
        self.assertEqual(self.get_segment("CAP-1")[1]["segment"]["cycle_id"], "boot-2")
        self.assertEqual(self.get_segment("CAP-2")[1]["segment"]["cycle_id"], "default")

    def test_reassign_unlocatable_when_target_coverage_insufficient(self):
        """目标周期锚点覆盖不足 -> 保持不可定位（UTC 字段为 null）。"""
        self.make_two_cycle_device()
        self.submit("CAP-OUT", "D1", 5000, 6000, "default")  # default 覆盖不足
        status, _ = self.get_segment("CAP-OUT")
        # default 覆盖 [1000,2000]，5000-6000 越界 -> 本就不可定位
        # 先把它放到可定位范围再移：改用一段 default 可定位、boot-2 不可定位的
        self.submit("CAP-1", "D1", 1500, 1600, "default")
        # 收窄 boot-2 锚点，使其不覆盖 1500-1600
        self.req("PUT", "/devices/D1/anchors", {
            "cycle_id": "boot-2",
            "anchors": [{"local_ms": 100, "utc_ms": 500000},
                        {"local_ms": 200, "utc_ms": 500100}]})
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        (seg,) = body["segments"]
        self.assertEqual(seg["cycle_id"], "boot-2")
        self.assertEqual(seg["status"], "unlocatable")
        self.assertIsNone(seg["utc_start_ms"])
        self.assertIsNone(seg["utc_end_ms"])

    def test_reassign_visible_in_timeline_and_listing(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        self.submit("CAP-2", "D1", 1300, 1400, "default")
        status, _ = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        # 当前时间轴反映新归属
        _, tl = self.req("GET", "/timeline/current")
        by_id = {s["capture_id"]: s for s in tl["segments"]}
        self.assertEqual(by_id["CAP-1"]["cycle_id"], "boot-2")
        self.assertEqual(by_id["CAP-1"]["utc_start_ms"], 500100)
        # 按周期过滤的声段列表同步变化
        _, lst = self.req("GET", "/segments?device_id=D1&cycle_id=boot-2")
        self.assertEqual([s["capture_id"] for s in lst["segments"]], ["CAP-1"])
        _, lst = self.req("GET", "/segments?device_id=D1&cycle_id=default")
        self.assertEqual([s["capture_id"] for s in lst["segments"]], ["CAP-2"])
        # 周期列表的声段计数同步变化
        _, cyc = self.req("GET", "/devices/D1/cycles")
        counts = {c["cycle_id"]: c["segment_count"] for c in cyc["cycles"]}
        self.assertEqual(counts, {"boot-2": 1, "default": 1})

    # ------------------------------------------------------------ 指纹更新

    def test_reassign_updates_cycle_fingerprints(self):
        self.make_two_cycle_device()
        self.reg_cycle("D1", "boot-3")
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        fp_default = self.fingerprint("D1", "default")
        fp_boot2 = self.fingerprint("D1", "boot-2")
        fp_boot3 = self.fingerprint("D1", "boot-3")

        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        # 响应携带相关周期（源+目标）更新后的指纹，且与周期详情一致
        fps = {(c["device_id"], c["cycle_id"]): c["fingerprint"]
               for c in body["cycles"]}
        self.assertEqual(set(fps), {("D1", "default"), ("D1", "boot-2")})
        self.assertNotEqual(fps[("D1", "default")], fp_default)
        self.assertNotEqual(fps[("D1", "boot-2")], fp_boot2)
        self.assertEqual(fps[("D1", "default")], self.fingerprint("D1", "default"))
        self.assertEqual(fps[("D1", "boot-2")], self.fingerprint("D1", "boot-2"))
        # 无关周期指纹不变
        self.assertEqual(self.fingerprint("D1", "boot-3"), fp_boot3)

    # ------------------------------------------------------------ 重传规则

    def test_replay_rules_after_reassign(self):
        """纠正后的同编号重传：新周期同内容幂等，旧周期或异内容冲突。"""
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default", note="鸟鸣")
        status, _ = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        # 新周期 + 原起止 + 原 note -> 同内容 -> 幂等
        status, body = self.submit("CAP-1", "D1", 1100, 1200, "boot-2", note="鸟鸣")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        self.assertEqual(body["segment"]["cycle_id"], "boot-2")
        # 旧周期重传 -> 异内容 -> 409 capture_conflict
        status, body = self.submit("CAP-1", "D1", 1100, 1200, "default", note="鸟鸣")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        self.assertEqual(body["error"]["details"]["existing"]["cycle_id"], "boot-2")
        # 新周期但内容不同 -> 409 capture_conflict
        status, body = self.submit("CAP-1", "D1", 1100, 1201, "boot-2", note="鸟鸣")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")
        # 均未产生新声段
        self.assertEqual(self.req("GET", "/segments")[1]["count"], 1)

    # ------------------------------------------------------------ 整批拒绝

    def test_unknown_capture_rejected_atomically(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        fp_default = self.fingerprint("D1", "default")
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-9999", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "capture_not_found")
        self.assertEqual(body["error"]["details"]["capture_ids"], ["CAP-9999"])
        # 整批拒绝：有效项也未移动，指纹不变
        self.assertEqual(self.get_segment("CAP-1")[1]["segment"]["cycle_id"], "default")
        self.assertEqual(self.fingerprint("D1", "default"), fp_default)

    def test_duplicate_capture_rejected(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"},
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "reassign_duplicate")
        self.assertEqual(body["error"]["details"]["capture_ids"], ["CAP-1"])
        self.assertEqual(self.get_segment("CAP-1")[1]["segment"]["cycle_id"], "default")

    def test_cycle_mismatch_rejected_atomically(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        self.submit("CAP-2", "D1", 1300, 1400, "default")
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "boot-2", "to_cycle_id": "default"},
            {"capture_id": "CAP-2", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "reassign_cycle_mismatch")
        (mm,) = body["error"]["details"]["mismatches"]
        self.assertEqual(mm["capture_id"], "CAP-1")
        self.assertEqual(mm["expected_cycle_id"], "boot-2")
        self.assertEqual(mm["actual_cycle_id"], "default")
        # 整批拒绝：第二项（本身有效）也未移动
        self.assertEqual(self.get_segment("CAP-2")[1]["segment"]["cycle_id"], "default")

    def test_target_cycle_not_found_rejected(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        # 目标周期未登记
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "ghost"}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        self.assertEqual(body["error"]["details"]["items"],
                         [{"capture_id": "CAP-1", "device_id": "D1",
                           "cycle_id": "ghost"}])
        self.assertEqual(self.get_segment("CAP-1")[1]["segment"]["cycle_id"], "default")

    def test_target_cycle_on_other_device_not_allowed(self):
        """目标周期只在别的设备上登记过 -> 仍视为未登记（只能在同设备周期之间移动）。"""
        self.make_two_cycle_device("D1")
        self.make_device("D2")
        self.reg_cycle("D2", "boot-9")
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-9"}])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")
        self.assertEqual(self.get_segment("CAP-1")[1]["segment"]["cycle_id"], "default")

    def test_sealed_source_or_target_rejected(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        self.submit("CAP-2", "D1", 1100, 1200, "boot-2")
        self.seal("D1", "default")
        # 源周期已封存 -> 409 cycle_sealed
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        self.assertEqual(body["error"]["details"]["cycles"][0]["cycle_id"], "default")
        # 目标周期已封存 -> 409 cycle_sealed
        status, body = self.reassign([
            {"capture_id": "CAP-2", "from_cycle_id": "boot-2", "to_cycle_id": "default"}])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 均未移动
        self.assertEqual(self.get_segment("CAP-1")[1]["segment"]["cycle_id"], "default")
        self.assertEqual(self.get_segment("CAP-2")[1]["segment"]["cycle_id"], "boot-2")

    def test_same_cycle_rejected(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        status, body = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "default"}])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "reassign_same_cycle")
        self.assertEqual(body["error"]["details"]["capture_ids"], ["CAP-1"])
        self.assertEqual(self.get_segment("CAP-1")[1]["segment"]["cycle_id"], "default")

    def test_validation_errors(self):
        self.make_two_cycle_device()
        # 缺 items / items 非数组 / 空批
        for bad in ({}, {"items": "x"}, {"items": []}):
            status, body = self.req("POST", "/segments/reassign-cycle", bad)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
        # 项缺字段 / 项非对象 / 编号非法
        for bad_items in (
                [{"capture_id": "CAP-1", "from_cycle_id": "default"}],
                ["not-an-object"],
                [{"capture_id": "bad id", "from_cycle_id": "default",
                  "to_cycle_id": "boot-2"}],
                [{"capture_id": "CAP-1", "from_cycle_id": "default",
                  "to_cycle_id": "bad id"}]):
            status, body = self.reassign(bad_items)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")

    def test_reassign_does_not_touch_other_segments(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        self.submit("CAP-2", "D1", 1300, 1400, "default")
        before = self.get_segment("CAP-2")[1]["segment"]
        status, _ = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        after = self.get_segment("CAP-2")[1]["segment"]
        self.assertEqual(after, before)  # 同周期其他声段完全不变

    # ------------------------------------------------------------ 与版本发布共存

    def test_published_versions_immutable_new_versions_reflect_reassign(self):
        self.make_two_cycle_device()
        self.submit("CAP-1", "D1", 1100, 1200, "default")
        status, v1 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        # 批量纠正归属
        status, _ = self.reassign([
            {"capture_id": "CAP-1", "from_cycle_id": "default", "to_cycle_id": "boot-2"}])
        self.assertEqual(status, 200)
        # 已发布版本保持原快照（旧归属、旧 UTC）
        _, v1_again = self.req("GET", "/versions/1")
        self.assertEqual(v1_again["segments"], v1["segments"])
        (seg,) = v1_again["segments"]
        self.assertEqual(seg["cycle_id"], "default")
        self.assertEqual(seg["utc_start_ms"], 100100)
        # 之后发布的版本反映新归属
        status, v2 = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        (seg,) = v2["segments"]
        self.assertEqual(seg["cycle_id"], "boot-2")
        self.assertEqual(seg["utc_start_ms"], 500100)
        # changes 体现位移（default 投影 -> boot-2 投影）
        (disp,) = v2["changes"]["displaced"]
        self.assertEqual(disp["capture_id"], "CAP-1")
        self.assertEqual(disp["previous"]["utc_start_ms"], 100100)
        self.assertEqual(disp["current"]["utc_start_ms"], 500100)
        self.assertEqual(disp["start_shift_ms"], 400000)
        self.assertEqual(v2["changes"]["added"], [])
        self.assertEqual(v2["changes"]["removed"], [])

    def test_concurrent_reassign_correct_and_publish(self):
        """批量纠正、锚点更正与发布并发：每个版本都是完整自洽的快照。"""
        self.make_two_cycle_device()
        for i in range(5):
            self.submit(f"CAP-C{i}", "D1", 1100 + i * 10, 1150 + i * 10, "default")

        results, errors = [], []

        def reassign(i):
            try:
                results.append(self.reassign([
                    {"capture_id": f"CAP-C{i}", "from_cycle_id": "default",
                     "to_cycle_id": "boot-2"}])[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def correct(k):
            try:
                results.append(self.req("PUT", "/devices/D1/anchors", {
                    "cycle_id": "boot-2",
                    "anchors": [{"local_ms": 1000, "utc_ms": 500000 + k * 100},
                                {"local_ms": 2000, "utc_ms": 501000 + k * 100}]})[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                results.append(self.req("POST", "/versions")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=reassign, args=(i,)))
            threads.append(threading.Thread(target=correct, args=(i,)))
            threads.append(threading.Thread(target=publish))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(all(s in (200, 201) for s in results))

        _, body = self.req("GET", "/versions")
        versions = body["versions"]
        self.assertGreaterEqual(len(versions), 1)
        # 版本号连续无空洞
        self.assertEqual([v["version_no"] for v in versions],
                         list(range(1, len(versions) + 1)))
        for v in versions:
            _, doc = self.req("GET", f"/versions/{v['version_no']}")
            self.assertEqual(doc["segment_count"], 5)
            # 快照内锚点按周期分组，便于核对自洽性
            anchors = {}
            for a in doc["anchors"]:
                anchors.setdefault(a["cycle_id"], {})[a["local_ms"]] = a["utc_ms"]
            for seg in doc["segments"]:
                cyc = anchors[seg["cycle_id"]]
                l0, l1 = sorted(cyc)
                # 声段 UTC 必须与快照内"所属周期"的锚点自洽（无撕裂读）
                expected = cyc[l0] + (seg["local_start_ms"] - l0) * \
                    (cyc[l1] - cyc[l0]) // (l1 - l0)
                self.assertEqual(seg["status"], "located")
                self.assertEqual(seg["utc_start_ms"], expected)
        # 全部纠正完成后，当前时间轴中 5 段都归属 boot-2
        _, tl = self.req("GET", "/timeline/current")
        self.assertEqual({s["cycle_id"] for s in tl["segments"]}, {"boot-2"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
