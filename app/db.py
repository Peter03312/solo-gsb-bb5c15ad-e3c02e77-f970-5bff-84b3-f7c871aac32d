"""SQLite 存储层：连接管理、表结构初始化、事务助手。

并发模型：
- 每个请求独立连接（WAL 模式，读写可并发）；
- 所有写操作包裹在 BEGIN IMMEDIATE 事务中，进入事务即取得写锁，
  保证"读取快照 -> 校验 -> 写入"是原子的；
- 发布版本时在同一个写事务内读取全部锚点与声段并落盘快照，
  因此每个版本必然对应一套完整、一致的锚点 + 声段快照。

周期（cycle）：
- anchors 的主键为 (device_id, cycle_id, local_ms)，同一设备不同周期可复用本地毫秒；
- segments / version_* 带 cycle_id 归属；
- versions.format_rev 标记快照格式：1=旧版（无周期字段，查询结果保持原样），
  2=周期版（锚点与声段投影带 cycle_id）。
- init_db 同时负责把 v1 旧库在线迁移到周期版结构（旧数据归入 default 周期）。

周期封存（seal，v1.2）：
- cycles.sealed_at / seal_fingerprint 记录封存状态与封存时核对的内容指纹；
- 旧库升级时自动补列，旧周期一律默认未封存。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_CYCLE_ID = "default"
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    device_id   TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    cycle_id    TEXT NOT NULL,
    note        TEXT,
    sealed_at   TEXT,           -- 封存时间（NULL = 未封存）；封存不可撤销
    seal_fingerprint TEXT,      -- 封存时核对的周期内容指纹（sha256 hex）
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, cycle_id)
);

CREATE TABLE IF NOT EXISTS anchors (
    device_id   TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    cycle_id    TEXT NOT NULL DEFAULT 'default',
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, cycle_id, local_ms)
);

CREATE TABLE IF NOT EXISTS segments (
    capture_id      TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL REFERENCES devices(device_id),
    cycle_id        TEXT NOT NULL DEFAULT 'default',
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_device ON segments(device_id);
CREATE INDEX IF NOT EXISTS idx_segments_cycle ON segments(device_id, cycle_id);

CREATE TABLE IF NOT EXISTS versions (
    version_no  INTEGER PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL,
    format_rev  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS version_anchors (
    version_no  INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
    device_id   TEXT NOT NULL,
    cycle_id    TEXT,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, cycle_id, local_ms)
);

CREATE TABLE IF NOT EXISTS version_segments (
    version_no      INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
    capture_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    cycle_id        TEXT,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL,          -- 'located' | 'unlocatable'
    utc_start_ms    INTEGER,
    utc_end_ms      INTEGER,
    created_at      TEXT,
    PRIMARY KEY (version_no, capture_id)
);
"""

# v1（无周期）库结构，仅用于迁移识别
_LEGACY_CYCLES_DDL = """
CREATE TABLE IF NOT EXISTS cycles (
    device_id   TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    cycle_id    TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, cycle_id)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy(conn: sqlite3.Connection) -> bool:
    """把 v1（无周期）库迁移到周期版结构。返回是否执行了迁移。

    - 旧 anchors 主键不含 cycle_id，必须整表重建，否则不同周期无法复用本地毫秒；
    - segments 主键仍是 capture_id，只需加列；旧声段/锚点一律归入 default 周期；
    - version_anchors 同样整表重建（cycle_id 留 NULL），version_segments 加可空列；
    - 已发布版本 format_rev=1，查询时仍按旧格式输出，结果不变；
    - 旧声段 content_hash 按新口径（含周期）重算，保证默认周期重传依旧幂等。
    """
    anchor_cols = _columns(conn, "anchors")
    if not anchor_cols or "cycle_id" in anchor_cols:
        return False  # 空库或已是周期版

    conn.execute(_LEGACY_CYCLES_DDL)
    # 为每台已有设备补登 default 周期
    conn.execute(
        "INSERT OR IGNORE INTO cycles(device_id, cycle_id, note, created_at) "
        "SELECT device_id, 'default', NULL, created_at FROM devices")

    # anchors 整表重建（主键变更）
    conn.execute(
        """CREATE TABLE anchors_new (
               device_id   TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
               cycle_id    TEXT NOT NULL DEFAULT 'default',
               local_ms    INTEGER NOT NULL,
               utc_ms      INTEGER NOT NULL,
               created_at  TEXT NOT NULL,
               PRIMARY KEY (device_id, cycle_id, local_ms)
           )""")
    conn.execute(
        "INSERT INTO anchors_new(device_id, cycle_id, local_ms, utc_ms, created_at) "
        "SELECT device_id, 'default', local_ms, utc_ms, created_at FROM anchors")
    conn.execute("DROP TABLE anchors")
    conn.execute("ALTER TABLE anchors_new RENAME TO anchors")

    # segments 加周期列（旧数据归 default）
    if "cycle_id" not in _columns(conn, "segments"):
        conn.execute("ALTER TABLE segments ADD COLUMN cycle_id TEXT NOT NULL DEFAULT 'default'")

    # versions 加格式版本列
    if "format_rev" not in _columns(conn, "versions"):
        conn.execute("ALTER TABLE versions ADD COLUMN format_rev INTEGER NOT NULL DEFAULT 1")

    # version_anchors 整表重建（主键含可空 cycle_id，历史快照 cycle_id=NULL）
    va_cols = _columns(conn, "version_anchors")
    if va_cols and "cycle_id" not in va_cols:
        conn.execute(
            """CREATE TABLE version_anchors_new (
                   version_no  INTEGER NOT NULL REFERENCES versions(version_no) ON DELETE CASCADE,
                   device_id   TEXT NOT NULL,
                   cycle_id    TEXT,
                   local_ms    INTEGER NOT NULL,
                   utc_ms      INTEGER NOT NULL,
                   PRIMARY KEY (version_no, device_id, cycle_id, local_ms)
               )""")
        conn.execute(
            "INSERT INTO version_anchors_new(version_no, device_id, cycle_id, local_ms, utc_ms) "
            "SELECT version_no, device_id, NULL, local_ms, utc_ms FROM version_anchors")
        conn.execute("DROP TABLE version_anchors")
        conn.execute("ALTER TABLE version_anchors_new RENAME TO version_anchors")

    # version_segments 加可空周期列
    vs_cols = _columns(conn, "version_segments")
    if vs_cols and "cycle_id" not in vs_cols:
        conn.execute("ALTER TABLE version_segments ADD COLUMN cycle_id TEXT")

    # 旧声段按新口径重算内容指纹（全部为 default 周期）
    from .logic import segment_content_hash
    rows = conn.execute(
        "SELECT capture_id, device_id, cycle_id, local_start_ms, local_end_ms, note "
        "FROM segments").fetchall()
    for r in rows:
        digest = segment_content_hash(
            r["capture_id"], r["device_id"], r["cycle_id"],
            r["local_start_ms"], r["local_end_ms"], r["note"])
        conn.execute("UPDATE segments SET content_hash=? WHERE capture_id=?",
                     (digest, r["capture_id"]))
    return True


def _ensure_seal_columns(conn: sqlite3.Connection) -> None:
    """周期封存列（v1.2）：为旧库的 cycles 表补齐 sealed_at / seal_fingerprint。

    旧行两列均为 NULL，即"默认未封存"，原有行为不变；
    封存成功时写入封存时间与核对的最终指纹，此后不可撤销。
    """
    cols = _columns(conn, "cycles")
    if not cols:
        return
    if "sealed_at" not in cols:
        conn.execute("ALTER TABLE cycles ADD COLUMN sealed_at TEXT")
    if "seal_fingerprint" not in cols:
        conn.execute("ALTER TABLE cycles ADD COLUMN seal_fingerprint TEXT")


def init_db(db_path: str) -> None:
    """初始化数据库（建表 + 旧库迁移）。服务启动时调用，幂等。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        # 迁移必须在显式写事务内逐条执行（executescript 会隐式提交，不能放入此事务）
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate_legacy(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        # 建表全部为 IF NOT EXISTS，天然幂等，可独立执行
        conn.executescript(SCHEMA)
        # 列级迁移：旧库的 cycles 表（含 v1 迁移而来的）补齐封存列
        _ensure_seal_columns(conn)
    finally:
        conn.close()


@contextmanager
def session(db_path: str):
    """打开一个连接并在结束时关闭。"""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE，进入即持有写锁，读写同一快照。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_txn(conn: sqlite3.Connection):
    """读事务：保证多条 SELECT 看到同一快照。"""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
