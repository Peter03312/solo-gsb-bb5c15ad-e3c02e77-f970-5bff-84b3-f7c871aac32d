"""时间轴对齐服务 Python 调用示例。

用法：先启动服务（python -m app 或 docker compose up），再运行：
    python3 examples/client.py [base_url]
默认连接 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


class TimelineClient:
    """时间轴对齐服务的极简客户端（仅标准库）。"""

    def __init__(self, base: str = "http://127.0.0.1:8000"):
        self.base = base.rstrip("/")

    def _call(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    # 设备
    def register_device(self, device_id: str, name: str | None = None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self._call("POST", "/devices", payload)

    # 时钟周期（设备重启轮次）
    def register_cycle(self, device_id: str, cycle_id: str, note: str | None = None):
        payload = {"cycle_id": cycle_id}
        if note is not None:
            payload["note"] = note
        return self._call("POST", f"/devices/{device_id}/cycles", payload)

    def list_cycles(self, device_id: str):
        return self._call("GET", f"/devices/{device_id}/cycles")

    def cycle_detail(self, device_id: str, cycle_id: str = "default"):
        """周期详情：内容稳定指纹 + 封存状态。"""
        return self._call("GET", f"/devices/{device_id}/cycles/{cycle_id}")

    def seal_cycle(self, device_id: str, cycle_id: str, fingerprint: str):
        """封存周期：携带周期详情中的内容指纹，不可撤销。"""
        return self._call("POST", f"/devices/{device_id}/cycles/{cycle_id}/seal",
                          {"fingerprint": fingerprint})

    # 锚点
    def add_anchor(self, device_id: str, local_ms: int, utc_ms: int,
                   cycle_id: str | None = None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self._call("POST", f"/devices/{device_id}/anchors", payload)

    def replace_anchors(self, device_id: str, anchors: list[dict],
                        cycle_id: str | None = None):
        payload = {"anchors": anchors}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self._call("PUT", f"/devices/{device_id}/anchors", payload)

    # 声段
    def submit_segment(self, capture_id: str, device_id: str,
                       local_start_ms: int, local_end_ms: int,
                       note: str | None = None, cycle_id: str | None = None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": local_start_ms, "local_end_ms": local_end_ms}
        if note is not None:
            payload["note"] = note
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self._call("POST", "/segments", payload)

    def delete_segment(self, capture_id: str):
        return self._call("DELETE", f"/segments/{capture_id}")

    # 周期归属批量纠正
    def reassign_cycles(self, items: list[dict]):
        """批量纠正声段周期归属：每项 {capture_id, from_cycle_id, to_cycle_id}。

        整批原子：任一项无效（编号不存在/同批重复/原周期不符/目标未登记/
        源或目标已封存/原周期等于目标周期）即全部拒绝，不改动任何数据。
        """
        return self._call("POST", "/segments/reassign-cycle", {"items": items})

    # 时间轴与版本
    def timeline(self):
        return self._call("GET", "/timeline/current")

    def publish(self, note: str | None = None):
        return self._call("POST", "/versions", {"note": note} if note else None)

    def version(self, version_no: int):
        return self._call("GET", f"/versions/{version_no}")


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:1200])


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    cli = TimelineClient(base)

    # 1. 注册两台录音设备
    show("注册设备 D1", *cli.register_device("D1", "录音笔-甲"))
    show("注册设备 D2", *cli.register_device("D2", "录音笔-乙"))

    # 2. 录入同步锚点（设备本地毫秒 <-> UTC 毫秒）
    #    D1：本地 1000ms 对时到 UTC 1700000000000，本地 61000ms 对时到 1700000060500
    #        （设备时钟每 1000ms 快约 8.3ms，线性漂移）
    show("D1 锚点#1", *cli.add_anchor("D1", 1000, 1700000000000))
    show("D1 锚点#2", *cli.add_anchor("D1", 61000, 1700000060500))
    show("D2 锚点#1", *cli.add_anchor("D2", 0, 1699999997000))
    show("D2 锚点#2", *cli.add_anchor("D2", 60000, 1700000057000))

    # 3. 提交声段（唯一采集编号 + 设备 + 本地起止毫秒）
    show("提交声段 CAP-0001", *cli.submit_segment("CAP-0001", "D1", 5000, 20000, note="鸟鸣"))
    show("提交声段 CAP-0002", *cli.submit_segment("CAP-0002", "D2", 3000, 9000))
    show("提交声段 CAP-0003(越界->无法定位)",
         *cli.submit_segment("CAP-0003", "D1", 90000, 95000))

    # 4. 幂等重传与冲突
    show("CAP-0001 同内容重传(幂等)",
         *cli.submit_segment("CAP-0001", "D1", 5000, 20000, note="鸟鸣"))
    show("CAP-0001 异内容重传(409 冲突)",
         *cli.submit_segment("CAP-0001", "D1", 5000, 20001, note="鸟鸣"))

    # 5. 发布版本 v1
    show("发布 v1", *cli.publish("初始对齐"))

    # 6. 锚点更正：D1 的 UTC 基准整体修正 +250ms，声段自动重算
    show("更正 D1 锚点", *cli.replace_anchors("D1", [
        {"local_ms": 1000, "utc_ms": 1700000000250},
        {"local_ms": 61000, "utc_ms": 1700000060750},
    ]))

    # 7. 发布 v2：changes 中可见 CAP-0001 的位移
    show("发布 v2", *cli.publish("修正 D1 对时"))

    # 8. 回看 v1：已发布版本保持不变
    show("回看 v1（不变）", *cli.version(1))

    # 9. 设备重启：本地毫秒归零，登记新周期 boot-2 并复用 local_ms
    show("登记周期 boot-2", *cli.register_cycle("D1", "boot-2", note="第二次开机"))
    show("周期列表", *cli.list_cycles("D1"))
    #    boot-2 周期在 local_ms=1000 处重新对时（UTC 与 default 完全不同）
    show("boot-2 锚点#1（复用 local_ms=1000）",
         *cli.add_anchor("D1", 1000, 1700010000000, cycle_id="boot-2"))
    show("boot-2 锚点#2",
         *cli.add_anchor("D1", 61000, 1700010060500, cycle_id="boot-2"))
    #    相同 local 起止的声段，只按 boot-2 周期锚点映射
    show("提交 boot-2 声段 CAP-0100",
         *cli.submit_segment("CAP-0100", "D1", 5000, 20000,
                             note="重启后录制", cycle_id="boot-2"))
    #    同采集编号换周期 -> 409（周期计入内容指纹）
    show("CAP-0001 换周期重传(409)",
         *cli.submit_segment("CAP-0001", "D1", 5000, 20000,
                             note="鸟鸣", cycle_id="boot-2"))

    # 10. 发布 v3：各周期锚点与声段投影原子冻结，文档带 cycle_id / format_rev=2
    show("发布 v3（双周期冻结）", *cli.publish("加入重启周期"))

    # 10.5 现场整理：CAP-0100 实际录在 default 周期（此前误记到 boot-2），
    #      按采集编号批量纠正周期归属（同一设备的已登记周期之间移动）
    show("批量纠正周期归属（CAP-0100: boot-2 -> default）",
         *cli.reassign_cycles([
             {"capture_id": "CAP-0100", "from_cycle_id": "boot-2",
              "to_cycle_id": "default"}]))
    #      纠正后同编号重传：新周期同内容 -> 幂等；按旧周期 -> 409 冲突
    show("纠正后新周期同内容重传（幂等）",
         *cli.submit_segment("CAP-0100", "D1", 5000, 20000, note="重启后录制"))
    show("纠正后按旧周期重传（409 冲突）",
         *cli.submit_segment("CAP-0100", "D1", 5000, 20000,
                             note="重启后录制", cycle_id="boot-2"))
    #      任一项无效（如此处 CAP-9999 不存在）-> 整批拒绝，不改动任何数据
    show("含不存在编号（整批拒绝）",
         *cli.reassign_cycles([
             {"capture_id": "CAP-0001", "from_cycle_id": "default",
              "to_cycle_id": "boot-2"},
             {"capture_id": "CAP-9999", "from_cycle_id": "default",
              "to_cycle_id": "boot-2"}]))

    # 11. 现场核对完成：封存 D1 的 default 周期，防止晚到记录改动已确认时间线
    #     周期详情返回内容稳定指纹与封存状态
    _, detail = cli.cycle_detail("D1", "default")
    show("D1/default 周期详情（指纹+封存状态）", 200, detail)
    fp = detail["cycle"]["fingerprint"]
    #     存在不可定位声段（CAP-0003 越界）-> 409，状态不变
    show("带不可定位声段封存（409）", *cli.seal_cycle("D1", "default", fp))
    #     移除该声段后旧指纹即过期 -> 409 seal_fingerprint_stale
    show("删除不可定位声段 CAP-0003", *cli.delete_segment("CAP-0003"))
    show("用过期指纹封存（409）", *cli.seal_cycle("D1", "default", fp))
    #     重新获取指纹并封存 -> 返回封存时间与最终指纹
    _, detail = cli.cycle_detail("D1", "default")
    fp = detail["cycle"]["fingerprint"]
    show("封存 D1/default", *cli.seal_cycle("D1", "default", fp))
    #     重复相同封存请求 -> 返回首次结果（幂等）
    show("重复相同封存请求（幂等）", *cli.seal_cycle("D1", "default", fp))
    #     封存后该周期锚点/声段变更一律 409 cycle_sealed
    show("封存后新增锚点（409）", *cli.add_anchor("D1", 30000, 1700000030000))
    show("封存后新声段（409）", *cli.submit_segment("CAP-0005", "D1", 1000, 2000))
    #     封存后该周期声段的周期归属也不能改 -> 409 cycle_sealed
    show("封存后批量纠正周期归属（409）",
         *cli.reassign_cycles([
             {"capture_id": "CAP-0001", "from_cycle_id": "default",
              "to_cycle_id": "boot-2"}]))
    #     同内容重传仍按幂等规则返回
    show("封存后同内容重传（幂等）",
         *cli.submit_segment("CAP-0001", "D1", 5000, 20000, note="鸟鸣"))
    #     未封存周期（boot-2）不受影响
    show("未封存周期正常写入",
         *cli.submit_segment("CAP-0101", "D1", 6000, 8000, cycle_id="boot-2"))


if __name__ == "__main__":
    main()
