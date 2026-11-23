"""SQLite 连接、表结构与旧库迁移。

表设计：
- meta            : 全局参数，当前仅有 pending_month（待结月）。
- batches         : 导入批次；period_month 记录导入时的待结月，决定该批计入哪一期封账。
- payments        : 付款当前状态，一笔付款 = (赔款号, 付款流水号)。
                    payment_ref 为空表示「无流水旧笔」；同一赔款号最多一笔旧笔。
                    assumed_from_legacy = 1 表示该流水承接了旧笔，是旧笔的延续。
- payment_archived: 每笔付款在最近一次封账时的归档快照，用于计算下期净变动。
                    以 payment_id 为键，承接改号时快照自然跟随，不产生净额。
- closes          : 已封账月份。
- archive         : 每期归档汇总，只保存净变动非零的 (事故月, 支付月) 组合；回查只读此表。

旧库（claims / claim_archived 以赔款号为键）在启动时自动迁移：
每笔旧赔案转成一条 payment_ref 为空的旧笔，归档快照按同一赔款号挂到新 payment_id 上。
"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  period_month  TEXT NOT NULL,
  row_count     INTEGER NOT NULL,
  total_cents   INTEGER NOT NULL,
  overwritten   INTEGER NOT NULL DEFAULT 0,
  assumed_count INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS payments (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_no             TEXT NOT NULL,
  payment_ref          TEXT,
  accident_month       TEXT NOT NULL,
  payment_month        TEXT NOT NULL,
  amount_cents         INTEGER NOT NULL CHECK (amount_cents >= 0),
  batch_id             INTEGER NOT NULL REFERENCES batches(id),
  assumed_from_legacy  INTEGER NOT NULL DEFAULT 0 CHECK (assumed_from_legacy IN (0, 1))
);

CREATE TABLE IF NOT EXISTS payment_archived (
  payment_id     INTEGER PRIMARY KEY REFERENCES payments(id),
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  amount_cents   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS closes (
  close_month TEXT PRIMARY KEY,
  batch_count INTEGER NOT NULL,
  combo_count INTEGER NOT NULL,
  closed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS archive (
  close_month    TEXT NOT NULL,
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  net_cents      INTEGER NOT NULL,
  PRIMARY KEY (close_month, accident_month, payment_month)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_ref
  ON payments(claim_no, payment_ref) WHERE payment_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_legacy
  ON payments(claim_no) WHERE payment_ref IS NULL;
CREATE INDEX IF NOT EXISTS idx_payments_batch ON payments(batch_id);
CREATE INDEX IF NOT EXISTS idx_payments_claim ON payments(claim_no);
CREATE INDEX IF NOT EXISTS idx_batches_period ON batches(period_month);
"""


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _column_names(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn):
    """为旧库补上新增列（不重建表，已有数据原样保留）。"""
    if "assumed_count" not in _column_names(conn, "batches"):
        conn.execute("ALTER TABLE batches ADD COLUMN assumed_count INTEGER NOT NULL DEFAULT 0")


def _migrate_legacy(conn):
    """把旧 claims / claim_archived 表迁移为 payments / payment_archived。

    只在旧表存在时执行；整个过程单事务，失败回滚，原库保持可用。
    """
    if not _table_exists(conn, "claims"):
        return
    has_archived = _table_exists(conn, "claim_archived")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 事务内复核：并发的另一进程可能已完成迁移或已写入新表
        if not _table_exists(conn, "claims"):
            conn.commit()
            return
        if conn.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"]:
            raise RuntimeError("检测到旧表 claims 与新表 payments 同时有数据，已中止迁移以免重复入账")
        conn.execute("ALTER TABLE claims RENAME TO claims_legacy_old")
        if has_archived:
            conn.execute("ALTER TABLE claim_archived RENAME TO claim_archived_legacy_old")
        conn.execute(
            "INSERT INTO payments(claim_no, payment_ref, accident_month, payment_month,"
            "                     amount_cents, batch_id, assumed_from_legacy)"
            " SELECT claim_no, NULL, accident_month, payment_month, amount_cents, batch_id, 0"
            " FROM claims_legacy_old"
        )
        if has_archived:
            conn.execute(
                "INSERT INTO payment_archived(payment_id, accident_month, payment_month, amount_cents)"
                " SELECT p.id, a.accident_month, a.payment_month, a.amount_cents"
                " FROM claim_archived_legacy_old a"
                " JOIN payments p ON p.claim_no = a.claim_no AND p.payment_ref IS NULL"
            )
        conn.execute("DROP TABLE claims_legacy_old")
        if has_archived:
            conn.execute("DROP TABLE claim_archived_legacy_old")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DB_PATH"], isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app):
    conn = sqlite3.connect(app.config["DB_PATH"], timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _migrate_legacy(conn)
        conn.commit()
    finally:
        conn.close()
