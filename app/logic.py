"""核心业务逻辑：导入批次、封账归档、三角回查。

付款模型：
- 一笔付款 = (赔款号, 付款流水号)；流水号为空表示「无流水旧笔」，同一赔款号至多一笔。
- 同一赔款号可有多笔流水，各自独立入账；同流水重报只覆盖该笔。
- 旧笔被某个流水承接后，该流水就是旧笔的延续（同一条 payments 记录、同一个归档快照），
  之后该赔款号的无流水重报仍更正这笔；承接只补流水号时封账不产生净额。
- 从未有过旧笔的赔款号，一旦出现流水，就拒收同号的无流水行（同批也算）。

并发与原子性约定：
- 所有写操作（设置待结月 / 导入批次 / 封账）先抢进程内 WRITE_LOCK，再在
  BEGIN IMMEDIATE 单事务内完成；SQLite 同时串行化写者。导入与封账重叠时，
  谁先提交谁先进库：批次按提交时的待结月计入本期或下期，失败整体回滚，
  不留部分结果。承接与整批入账在同一事务内提交。
"""
import csv
import io
import re
import sqlite3
import threading

from .months import month_range, next_month, parse_month

WRITE_LOCK = threading.Lock()

MAX_AMOUNT_CENTS = 999_999_999_999_999  # 15 位以内，避免 SUM 溢出 int64
CLAIM_NO_RE = re.compile(r"^[^\s,]{1,64}$")
AMOUNT_RE = re.compile(r"^\d{1,15}$")

HEADER_ALIASES = {
    "claim_no": {"claim_no", "claimno", "claim", "claim_id", "赔款号", "赔案号"},
    "accident_month": {"accident_month", "accident", "accidentmonth", "事故月"},
    "payment_month": {"payment_month", "payment", "paymentmonth", "支付月"},
    "amount_cents": {"amount_cents", "amount", "amountcent", "金额", "金额分", "金额(分)", "金额（分）"},
}
# 可选列：付款流水号 / 承接旧笔（1 表示承接）
OPTIONAL_ALIASES = {
    "payment_ref": {"payment_ref", "paymentref", "payment_no", "paymentno", "ref",
                    "流水号", "付款流水号", "付款流水"},
    "assume_legacy": {"assume_legacy", "assume", "legacy", "承接旧笔", "承接"},
}
ALL_ALIASES = {**HEADER_ALIASES, **OPTIONAL_ALIASES}


class BatchError(Exception):
    """整批校验/写入失败，errors 为行级错误列表；不会产生部分结果。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors[:5]))


class CloseError(Exception):
    """封账请求非法（如只能封当前待结月）。"""


# ---------------------------------------------------------------- 待结月

def get_pending(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'pending_month'").fetchone()
    return row["value"] if row else None


def _set_pending(conn, month):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('pending_month', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (month,),
    )


def has_any_data(conn):
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM batches) + (SELECT COUNT(*) FROM closes) AS n"
    ).fetchone()
    return row["n"] > 0


def set_first_pending(conn, month):
    """设置首个待结月；已存在任何批次或封账数据后禁止修改。调用方须持有 WRITE_LOCK。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if has_any_data(conn):
            raise CloseError("已存在批次或封账数据，不能再修改待结月")
        _set_pending(conn, month)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- CSV 解析校验

def _parse_assume(value):
    """承接旧笔：空 / 0 -> False，1 -> True，其它非法返回 None。"""
    v = value.strip()
    if v in ("", "0"):
        return False
    if v == "1":
        return True
    return None


def parse_csv_text(text, pending):
    """解析并整批校验 CSV 文本，返回行列表；任何错误抛 BatchError，不产生部分结果。

    pending 为当前待结月，用于校验 支付月 ∈ [事故月, 待结月]。
    本函数只做语法/批内结构校验；跨批次的承接、同笔冲突由 apply_batch 在事务内复核。
    """
    errors, rows = [], []
    seen_ref, seen_legacy = {}, {}
    header = col_map = None
    reader = csv.reader(io.StringIO(text))
    for lineno, raw in enumerate(reader, start=1):
        if not raw or all(not c.strip() for c in raw):
            continue
        if header is None:
            header = [c.strip().lstrip("﻿").lower() for c in raw]
            col_map = {}
            for idx, name in enumerate(header):
                field = next((f for f, aliases in ALL_ALIASES.items() if name in aliases), None)
                if field is None:
                    errors.append(f"表头第 {idx + 1} 列无法识别：{name!r}")
                elif field in col_map.values():
                    errors.append(f"表头列重复：{name!r}")
                else:
                    col_map[idx] = field
            missing = sorted(set(HEADER_ALIASES) - set(col_map.values()))
            if missing:
                errors.append("缺少必需列：" + ", ".join(missing))
            if errors:
                raise BatchError(errors)
            continue
        if len(raw) != len(header):
            errors.append(f"第 {lineno} 行：列数 {len(raw)} 与表头 {len(header)} 不一致")
            continue
        rec = {col_map[i]: raw[i].strip() for i in range(len(header))}
        claim_no = rec["claim_no"]
        no_ok = bool(CLAIM_NO_RE.match(claim_no))
        if not no_ok:
            errors.append(f"第 {lineno} 行：赔款号为空或含空格/逗号等非法字符：{claim_no!r}")

        ref_raw = rec.get("payment_ref", "")
        payment_ref = ref_raw or None
        if payment_ref is not None and not CLAIM_NO_RE.match(payment_ref):
            errors.append(f"第 {lineno} 行：付款流水号为空或含空格/逗号等非法字符：{payment_ref!r}")
            ref_ok = False
        else:
            ref_ok = True

        assume = _parse_assume(rec.get("assume_legacy", ""))
        if assume is None:
            errors.append(f"第 {lineno} 行：承接旧笔须为空、0 或 1：{rec.get('assume_legacy')!r}")
        elif assume and payment_ref is None:
            errors.append(f"第 {lineno} 行：标记承接旧笔时必须填写付款流水号")

        if no_ok and ref_ok:
            if payment_ref is None:
                if claim_no in seen_legacy:
                    errors.append(
                        f"第 {lineno} 行：赔款号 {claim_no} 的无流水行与本批第 "
                        f"{seen_legacy[claim_no]} 行重复（两行指向同一笔付款）")
                else:
                    seen_legacy[claim_no] = lineno
            else:
                key = (claim_no, payment_ref)
                if key in seen_ref:
                    errors.append(
                        f"第 {lineno} 行：赔款号 {claim_no} 的流水 {payment_ref} 与本批第 "
                        f"{seen_ref[key]} 行重复（两行指向同一笔付款）")
                else:
                    seen_ref[key] = lineno

        acc = parse_month(rec["accident_month"])
        if acc is None:
            errors.append(f"第 {lineno} 行：事故月格式应为 YYYY-MM：{rec['accident_month']!r}")
        pay = parse_month(rec["payment_month"])
        if pay is None:
            errors.append(f"第 {lineno} 行：支付月格式应为 YYYY-MM：{rec['payment_month']!r}")
        amt = None
        if not AMOUNT_RE.match(rec["amount_cents"]):
            errors.append(f"第 {lineno} 行：金额须为非负整数（分）：{rec['amount_cents']!r}")
        else:
            amt = int(rec["amount_cents"])
            if amt > MAX_AMOUNT_CENTS:
                errors.append(f"第 {lineno} 行：金额超出上限 {MAX_AMOUNT_CENTS} 分")
        if acc and pay:
            if pay < acc:
                errors.append(f"第 {lineno} 行：支付月 {pay} 早于事故月 {acc}")
            elif pending and pay > pending:
                errors.append(f"第 {lineno} 行：支付月 {pay} 晚于当前待结月 {pending}")
        if no_ok and ref_ok and acc and pay and amt is not None and assume is not None \
                and not (assume and payment_ref is None):
            rows.append({"lineno": lineno, "claim_no": claim_no,
                         "payment_ref": payment_ref, "assume_legacy": bool(assume),
                         "accident_month": acc, "payment_month": pay,
                         "amount_cents": amt})
        if len(errors) > 100:
            errors.append("错误过多，其余已截断")
            break
    if header is None:
        errors.append("CSV 为空或缺少表头")
    if errors:
        raise BatchError(errors)
    if not rows:
        raise BatchError(["没有任何数据行"])
    return rows


# ---------------------------------------------------------------- 整批入账计划

def _plan_batch(conn, rows):
    """在事务内根据库中现有付款，把每行解析到唯一目标付款并检查冲突。

    返回 (ops, overwritten, assumed_count)。冲突/非法抛 BatchError，调用方回滚。
    target: ('pay', id) 现有付款；('new_ref', claim_no, ref) 新流水；
            ('new_legacy', claim_no) 新无流水旧笔。
    """
    errors = []
    ops = []
    groups = {}
    for row in rows:
        groups.setdefault(row["claim_no"], []).append(row)

    for claim_no, group in groups.items():
        existing = conn.execute(
            "SELECT id, payment_ref, assumed_from_legacy, accident_month, payment_month,"
            "       amount_cents FROM payments WHERE claim_no = ? ORDER BY id",
            (claim_no,),
        ).fetchall()
        legacy = [p for p in existing if p["payment_ref"] is None]
        assumed = [p for p in existing if p["assumed_from_legacy"]]
        ref_by_value = {p["payment_ref"]: p for p in existing if p["payment_ref"] is not None}
        if len(legacy) > 1 or len(assumed) > 1:
            errors.append(f"赔款号 {claim_no}：旧笔/承接记录不唯一，数据异常，整批退回")
            continue
        legacy = legacy[0] if legacy else None
        assumed = assumed[0] if assumed else None
        if legacy is not None and (assumed is not None or ref_by_value):
            # 正常状态不可能同时存在无流水旧笔与流水付款（承接会复用同一条记录）
            errors.append(
                f"赔款号 {claim_no}：同时存在无流水旧笔与流水付款，数据异常，整批退回")
            continue

        no_ref_rows = [r for r in group if r["payment_ref"] is None]
        ref_rows = [r for r in group if r["payment_ref"] is not None]
        assume_rows = [r for r in ref_rows if r["assume_legacy"]]

        if len(no_ref_rows) > 1:
            errors.append(
                f"赔款号 {claim_no}：第 {no_ref_rows[0]['lineno']} 行与第 "
                f"{no_ref_rows[1]['lineno']} 行都无流水，指向同一笔付款，整批退回")
        if len(assume_rows) > 1:
            errors.append(
                f"赔款号 {claim_no}：第 {assume_rows[0]['lineno']} 行与第 "
                f"{assume_rows[1]['lineno']} 行都标记承接旧笔，承接须恰好一条，整批退回")

        for r in assume_rows:
            if assumed is not None:
                # 旧笔已被承接后，账上不再有「无流水旧笔」，不允许再标记承接
                errors.append(
                    f"第 {r['lineno']} 行：赔款号 {claim_no} 的旧笔已由流水 "
                    f"{assumed['payment_ref']} 承接，不能改绑或再次标记承接")
            elif legacy is None:
                hint = "（同批新建的旧笔不能承接，承接只针对库中已有的旧笔）" if no_ref_rows else ""
                errors.append(
                    f"第 {r['lineno']} 行：赔款号 {claim_no} 没有无流水旧笔，"
                    f"不能标记承接旧笔{hint}")
            elif r["payment_ref"] in ref_by_value:
                errors.append(
                    f"第 {r['lineno']} 行：流水 {r['payment_ref']} 已属于赔款号 "
                    f"{claim_no} 的另一笔付款，不能用于承接旧笔")

        if legacy is not None and ref_rows and not assume_rows:
            errors.append(
                f"赔款号 {claim_no} 已有无流水旧笔：首次报送流水须在本批指定恰好一条"
                f"「承接旧笔=1」来承接它，整批退回")
        if legacy is not None and assume_rows and no_ref_rows:
            errors.append(
                f"第 {assume_rows[0]['lineno']} 行与第 {no_ref_rows[0]['lineno']} 行"
                f"最终指向同一笔付款（承接旧笔 + 无流水），整批退回")

        # 无流水行：旧笔 / 已承接付款 / 新旧笔，否则拒收
        for r in no_ref_rows:
            if legacy is not None:
                ops.append({"row": r, "target": ("pay", legacy["id"]), "assume": False})
            elif assumed is not None:
                ops.append({"row": r, "target": ("pay", assumed["id"]), "assume": False})
            elif ref_rows or existing:
                errors.append(
                    f"第 {r['lineno']} 行：赔款号 {claim_no} 从未有过无流水旧笔"
                    f"（已报送流水），拒收该号的无流水行")
            else:
                ops.append({"row": r, "target": ("new_legacy", claim_no), "assume": False})

        # 有流水行
        for r in ref_rows:
            ref = r["payment_ref"]
            if r["assume_legacy"] and legacy is not None:
                if ref not in ref_by_value:  # 已属于其它付款的流水不能改绑
                    ops.append({"row": r, "target": ("pay", legacy["id"]), "assume": True})
            elif ref in ref_by_value:
                ops.append({"row": r, "target": ("pay", ref_by_value[ref]["id"]), "assume": False})
            else:
                ops.append({"row": r, "target": ("new_ref", claim_no, ref), "assume": False})

    # 两行最终指向同一笔付款：整批退回，与行序无关。
    # 承接行与同批同号同流水的新流水行，承接后也是同一笔，一并视为冲突。
    def identity(op):
        kind = op["target"][0]
        if kind == "new_ref":
            return ("ref", op["target"][1], op["target"][2])
        if kind == "new_legacy":
            return ("legacy", op["target"][1])
        if op["assume"]:
            return ("ref", op["row"]["claim_no"], op["row"]["payment_ref"])
        return ("pay", op["target"][1])

    by_target = {}
    for op in ops:
        key = identity(op)
        prev = by_target.get(key)
        if prev is not None:
            errors.append(
                f"第 {prev['row']['lineno']} 行与第 {op['row']['lineno']} 行最终指向同一笔付款，"
                f"整批退回")
        else:
            by_target[key] = op

    if errors:
        raise BatchError(errors)
    overwritten = sum(1 for op in ops if op["target"][0] == "pay" and not op["assume"])
    assumed_count = sum(1 for op in ops if op["assume"])
    return ops, overwritten, assumed_count


def apply_batch(conn, rows):
    """整批写入（流水分笔、同流水覆盖、承接旧笔）。调用方须持有 WRITE_LOCK。

    所有校验、承接、写入在同一个 BEGIN IMMEDIATE 事务内完成；失败整体回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        pend = get_pending(conn)
        if pend is None:
            raise BatchError(["尚未设置待结月"])
        # 事务内按最新待结月复核（导入与封账可能交错）
        for r in rows:
            if r["payment_month"] > pend:
                raise BatchError([
                    f"支付月 {r['payment_month']} 晚于当前待结月 {pend}"
                    f"（期间封账已推进，请重新提交）"
                ])
        ops, overwritten, assumed_count = _plan_batch(conn, rows)
        total = sum(r["amount_cents"] for r in rows)
        cur = conn.execute(
            "INSERT INTO batches(period_month, row_count, total_cents, overwritten,"
            "                    assumed_count) VALUES(?,?,?,0,?)",
            (pend, len(rows), total, assumed_count),
        )
        batch_id = cur.lastrowid
        for op in ops:
            r = op["row"]
            kind = op["target"][0]
            if kind == "pay":
                payment_id = op["target"][1]
                if op["assume"]:
                    cur = conn.execute(
                        "UPDATE payments SET payment_ref = ?, accident_month = ?,"
                        "       payment_month = ?, amount_cents = ?, batch_id = ?,"
                        "       assumed_from_legacy = 1"
                        " WHERE id = ? AND payment_ref IS NULL",
                        (r["payment_ref"], r["accident_month"], r["payment_month"],
                         r["amount_cents"], batch_id, payment_id),
                    )
                    if cur.rowcount != 1:
                        raise BatchError([
                            f"第 {r['lineno']} 行：承接失败（旧笔状态已变化），整批退回"])
                else:
                    cur = conn.execute(
                        "UPDATE payments SET accident_month = ?, payment_month = ?,"
                        "       amount_cents = ?, batch_id = ? WHERE id = ?",
                        (r["accident_month"], r["payment_month"], r["amount_cents"],
                         batch_id, payment_id),
                    )
                    if cur.rowcount != 1:
                        raise BatchError([
                            f"第 {r['lineno']} 行：付款状态已变化，整批退回"])
            elif kind == "new_ref":
                conn.execute(
                    "INSERT INTO payments(claim_no, payment_ref, accident_month, payment_month,"
                    "                     amount_cents, batch_id, assumed_from_legacy)"
                    " VALUES(?,?,?,?,?,?,0)",
                    (r["claim_no"], r["payment_ref"], r["accident_month"],
                     r["payment_month"], r["amount_cents"], batch_id),
                )
            else:  # new_legacy
                conn.execute(
                    "INSERT INTO payments(claim_no, payment_ref, accident_month, payment_month,"
                    "                     amount_cents, batch_id, assumed_from_legacy)"
                    " VALUES(?,NULL,?,?,?,?,0)",
                    (r["claim_no"], r["accident_month"], r["payment_month"],
                     r["amount_cents"], batch_id),
                )
        conn.execute("UPDATE batches SET overwritten = ? WHERE id = ?", (overwritten, batch_id))
        conn.commit()
        return {"batch_id": batch_id, "period_month": pend, "row_count": len(rows),
                "total_cents": total, "overwritten": overwritten,
                "assumed_count": assumed_count}
    except BatchError:
        conn.rollback()
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise BatchError([f"数据库写入失败，整批已退回：{exc}"])
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- 封账

def _period_deltas(conn, pend):
    """本期（待结月 = pend）各 (事故月, 支付月) 组合的净变动。

    对本期批次触及的每笔付款：减去其归档快照（如有），加上当前值。
    承接旧笔复用同一 payment_id，快照仍指向旧笔上次封账的金额与月份，
    因此只补流水号不会产生净额。返回 (触及付款行, {组合: 净变动})，零变动已剔除。
    """
    touched = conn.execute(
        "SELECT p.id, p.claim_no, p.payment_ref, p.accident_month, p.payment_month,"
        "       p.amount_cents,"
        "       a.accident_month AS p_acc, a.payment_month AS p_pay, a.amount_cents AS p_amt"
        " FROM payments p"
        " JOIN batches b ON b.id = p.batch_id"
        " LEFT JOIN payment_archived a ON a.payment_id = p.id"
        " WHERE b.period_month = ?",
        (pend,),
    ).fetchall()
    deltas = {}
    for t in touched:
        if t["p_amt"] is not None:
            key = (t["p_acc"], t["p_pay"])
            deltas[key] = deltas.get(key, 0) - t["p_amt"]
        key = (t["accident_month"], t["payment_month"])
        deltas[key] = deltas.get(key, 0) + t["amount_cents"]
    return touched, {k: v for k, v in deltas.items() if v != 0}


def close_preview(conn, pend):
    touched, deltas = _period_deltas(conn, pend)
    batch_count = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE period_month = ?", (pend,)
    ).fetchone()["c"]
    items = [(acc, pay, f"{net:+,}") for (acc, pay), net in sorted(deltas.items())]
    return {"batch_count": batch_count, "payment_count": len(touched), "combos": items}


def close_pending(conn, month):
    """封当前待结月并推进到次月。调用方须持有 WRITE_LOCK。失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        pend = get_pending(conn)
        if pend is None:
            raise CloseError("尚未设置待结月")
        if month != pend:
            raise CloseError(f"只能封当前待结月 {pend}，不能封 {month}")
        touched, deltas = _period_deltas(conn, pend)
        for (acc, pay), net in sorted(deltas.items()):
            conn.execute(
                "INSERT INTO archive(close_month, accident_month, payment_month, net_cents)"
                " VALUES(?,?,?,?)",
                (pend, acc, pay, net),
            )
        for t in touched:
            conn.execute(
                "INSERT INTO payment_archived(payment_id, accident_month, payment_month,"
                "                             amount_cents)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(payment_id) DO UPDATE SET"
                "   accident_month = excluded.accident_month,"
                "   payment_month  = excluded.payment_month,"
                "   amount_cents   = excluded.amount_cents",
                (t["id"], t["accident_month"], t["payment_month"], t["amount_cents"]),
            )
        batch_count = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE period_month = ?", (pend,)
        ).fetchone()["c"]
        conn.execute(
            "INSERT INTO closes(close_month, batch_count, combo_count) VALUES(?,?,?)",
            (pend, batch_count, len(deltas)),
        )
        _set_pending(conn, next_month(pend))
        conn.commit()
        return {"closed": pend, "next": next_month(pend), "batch_count": batch_count,
                "combo_count": len(deltas), "payment_count": len(touched)}
    except CloseError:
        conn.rollback()
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise CloseError(f"封账写入失败，已整体回滚：{exc}")
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- 回查（只读归档汇总）

def closed_months(conn):
    return [r["close_month"] for r in
            conn.execute("SELECT close_month FROM closes ORDER BY close_month")]


def archive_detail(conn, close_month):
    return conn.execute(
        "SELECT accident_month, payment_month, net_cents FROM archive"
        " WHERE close_month = ? ORDER BY accident_month, payment_month",
        (close_month,),
    ).fetchall()


def build_triangle(conn, start, close_month):
    """按 close_month 封账时的口径重建累计赔付三角（只读 archive 汇总表）。

    行：起始事故月 start 至 close_month 的每个事故月；
    列：start 至最新已结月。每行从事故当月起逐月累计；
    事故月之前为 None（不适用），封账月之后留空。
    """
    rows = conn.execute(
        "SELECT accident_month, payment_month, SUM(net_cents) AS s"
        " FROM archive"
        " WHERE close_month <= ? AND accident_month >= ? AND accident_month <= ?"
        " GROUP BY accident_month, payment_month",
        (close_month, start, close_month),
    ).fetchall()
    cells = {(r["accident_month"], r["payment_month"]): r["s"] for r in rows}
    latest = conn.execute("SELECT MAX(close_month) AS m FROM closes").fetchone()["m"]
    columns = month_range(start, latest)
    table = []
    for acc in month_range(start, close_month):
        cum = 0
        out = []
        for col in columns:
            if col < acc:
                out.append({"kind": "na"})
            elif col <= close_month:
                cum += cells.get((acc, col), 0)
                out.append({"kind": "val", "text": f"{cum:,}"})
            else:
                out.append({"kind": "blank"})
        table.append({"accident": acc, "cells": out})
    return {"columns": columns, "rows": table, "start": start, "close": close_month}
