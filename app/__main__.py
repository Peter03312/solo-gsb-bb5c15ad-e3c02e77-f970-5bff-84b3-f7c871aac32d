"""服务入口：python -m app

启动时自动初始化数据库（建表，幂等），然后监听 HTTP。
配置通过环境变量：TIMELINE_HOST / TIMELINE_PORT / TIMELINE_DB_PATH。
"""
from __future__ import annotations

import os
import sys

from . import __version__, db, server


def main() -> int:
    host = os.environ.get("TIMELINE_HOST", "0.0.0.0")
    port = int(os.environ.get("TIMELINE_PORT", "8000"))
    db_path = os.environ.get(
        "TIMELINE_DB_PATH",
        os.path.join(os.getcwd(), "data", "timeline.db"))

    db.init_db(db_path)
    httpd = server.make_server(host, port, db_path)
    print(f"[timeline-align v{__version__}] 服务已启动: http://{host}:{port}", flush=True)
    print(f"[timeline-align] 数据库: {os.path.abspath(db_path)}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[timeline-align] 正在关闭...", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
