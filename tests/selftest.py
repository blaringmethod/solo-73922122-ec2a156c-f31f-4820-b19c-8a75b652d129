#!/usr/bin/env python3
"""关键操作自测（不依赖 pytest）。

覆盖：设置待结月、CSV 校验、封账推进、同流水覆盖与多流水分笔、
承接旧笔（首次承接/无净额/后续无流水更正/不可改绑/只能承接旧笔）、
无旧笔赔款号拒收无流水、同批混填与同笔冲突（与行序无关）、
整批原子性、旧库迁移、两期回查一致性、零额显示、导入/封账并发。

运行：python3 tests/selftest.py
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_CSV = os.path.join(REPO, "demo", "claims_2026-01_base.csv")
SUPP_CSV = os.path.join(REPO, "demo", "claims_2026-02_supplement.csv")

_passed, _failed = [], []


def check(name, cond, detail=""):
    (_passed if cond else _failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def make_client():
    tmp = tempfile.mkdtemp(prefix="lt-test-")
    app = create_app({"DB_PATH": os.path.join(tmp, "t.db"), "TESTING": True})
    return app, app.test_client(), app.config["DB_PATH"]


def q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def setup_pending(client, month="2026-01"):
    return client.post("/setup", data={"pending_month": month}, follow_redirects=True)


def import_text(client, text):
    return client.post("/import", data={"csv_text": text})


def close_month(client, month):
    return client.post("/close", data={"close_month": month}, follow_redirects=True)


HEADER = "claim_no,accident_month,payment_month,amount_cents\n"
H6 = "claim_no,accident_month,payment_month,amount_cents,payment_ref,assume_legacy\n"


def archive_map(db, close_month):
    return dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month=?",
        (close_month,)))


# ---------------------------------------------------------------- 1. 全流程 + 两期回查
def test_full_flow_two_periods():
    print("\n[1] 完整流程：基础批 -> 封账 -> 补报/更正 -> 封账 -> 两期回查")
    app, client, db = make_client()
    r = setup_pending(client)
    check("设置首个待结月 2026-01", "首个待结月已设为 2026-01" in r.get_data(as_text=True))

    r = import_text(client, open(BASE_CSV, encoding="utf-8").read())
    check("基础批导入成功", r.status_code == 200 and "批次 #1" in r.get_data(as_text=True),
          f"status={r.status_code}")
    check("基础批计入 2026-01 期", "计入 2026-01 期" in r.get_data(as_text=True))

    r = close_month(client, "2026-02")
    check("封非待结月被拒绝", "只能封当前待结月 2026-01" in r.get_data(as_text=True))
    check("拒绝后待结月仍为 2026-01",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-01")

    r = close_month(client, "2026-01")
    check("封 2026-01 成功并推进", "已封账 2026-01" in r.get_data(as_text=True)
          and "2026-02" in r.get_data(as_text=True))
    rows = q(db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-01'")
    check("2026-01 期归档 7 个组合", len(rows) == 7, f"got {len(rows)}")
    check("基础批 7 笔均为无流水旧笔",
          q(db, "SELECT COUNT(*) FROM payments WHERE payment_ref IS NULL")[0][0] == 7)

    r = import_text(client, open(SUPP_CSV, encoding="utf-8").read())
    body = r.get_data(as_text=True)
    check("补报批计入 2026-02 期", "计入 2026-02 期" in body)
    check("补报批覆盖重报 3 笔", "覆盖重报 3 笔" in body)

    r = close_month(client, "2026-02")
    check("封 2026-02 成功", "已封账 2026-02" in r.get_data(as_text=True))
    expected = {
        ("2025-10", "2025-12"): 15000,    # C-0002 更正金额 80000 -> 95000
        ("2025-11", "2026-01"): -60000,   # C-0004 支付月 2026-01 -> 2026-02
        ("2025-11", "2026-02"): 105000,   # C-0004 移入 60000 + C-0008 补报 45000
        ("2025-12", "2026-02"): 70000,    # C-0009 补报
        ("2026-01", "2026-02"): 110000,   # C-0010 补报
        ("2026-02", "2026-02"): 180000,   # C-0011 新事故月
    }
    check("2026-02 期仅归档 6 个净变动组合（同数重报 C-0007 不归档）",
          archive_map(db, "2026-02") == expected, f"got {archive_map(db, '2026-02')}")
    check("无流水行重报仍更正旧笔（C-0002 金额已更新）",
          q(db, "SELECT amount_cents FROM payments WHERE claim_no='C-0002'") == [(95000,)])

    t1 = client.get("/triangle?start=2025-10&close=2026-01").get_data(as_text=True)
    check("一期三角含旧值 200,000 / 210,000 / 290,000",
          all(s in t1 for s in ("200,000", "210,000", "290,000")))
    check("一期三角不含补报后的 215,000", "215,000" not in t1)
    check("一期三角封账月之后留空（4 个空白格）",
          t1.count('<td class="blank"></td>') == 4, f"got {t1.count('blank')}")

    t2 = client.get("/triangle?start=2025-10&close=2026-02").get_data(as_text=True)
    check("二期三角含更正后 215,000 / 255,000 / 360,000 / 180,000",
          all(s in t2 for s in ("215,000", "255,000", "360,000", "180,000")))
    check("二期三角无空白格（已结到最新月）", '<td class="blank"></td>' not in t2)

    t1b = client.get("/triangle?start=2025-10&close=2026-01").get_data(as_text=True)
    check("两期回查：一期三角在二期封账后保持原样", t1b == t1)

    a2 = client.get("/archive/2026-02").get_data(as_text=True)
    check("归档明细页展示 2026-02 期净变动", "2025-11" in a2 and "-60,000" in a2)


# ---------------------------------------------------------------- 1b. 演示分期批
def test_demo_installment_batch():
    print("\n[1b] 演示分期/承接批：承接无净额、新流水独立、覆盖计数不混淆")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, open(BASE_CSV, encoding="utf-8").read())
    close_month(client, "2026-01")
    import_text(client, open(SUPP_CSV, encoding="utf-8").read())
    close_month(client, "2026-02")
    r = import_text(client, open(
        os.path.join(REPO, "demo", "claims_2026-03_installments.csv"), encoding="utf-8").read())
    body = r.get_data(as_text=True)
    check("演示分期批导入成功", r.status_code == 200 and "批次 #3" in body, f"status={r.status_code}")
    check("演示分期批承接 2 笔、无普通覆盖",
          "承接旧笔 2 笔" in body and "覆盖重报 0 笔" in body, body[:300])
    close_month(client, "2026-03")
    check("2026-03 归档 3 个净变动组合（承接无净额）",
          archive_map(db, "2026-03") == {("2025-12", "2026-03"): 25000,
                                         ("2026-02", "2026-03"): 30000,
                                         ("2026-03", "2026-03"): 40000},
          f"got {archive_map(db, '2026-03')}")
    r = import_text(client, open(
        os.path.join(REPO, "demo", "claims_2026-03_conflict.csv"), encoding="utf-8").read())
    check("演示冲突批整批退回", r.status_code == 409
          and "同一笔付款" in r.get_data(as_text=True))
    check("冲突批未新增批次", len(q(db, "SELECT * FROM batches")) == 3)


# ---------------------------------------------------------------- 2. CSV 校验
def test_import_validation():
    print("\n[2] CSV 校验：非法行整批拒绝")
    app, client, db = make_client()
    setup_pending(client)
    cases = {
        "负数金额": HEADER + "C1,2025-12,2026-01,-100\n",
        "小数金额": HEADER + "C1,2025-12,2026-01,10.5\n",
        "非法月份": HEADER + "C1,2025-13,2026-01,100\n",
        "支付月早于事故月": HEADER + "C1,2026-01,2025-12,100\n",
        "支付月晚于待结月": HEADER + "C1,2025-12,2026-02,100\n",
        "同流水批内重复": H6 + "C1,2025-12,2026-01,100,R1,0\nC1,2025-12,2026-01,200,R1,0\n",
        "无流水批内重复": H6 + "C1,2025-12,2026-01,100,,\nC1,2025-12,2026-01,200,,\n",
        "承接标记非法": H6 + "C1,2025-12,2026-01,100,R1,2\n",
        "承接未填流水": H6 + "C1,2025-12,2026-01,100,,1\n",
        "流水号含逗号": H6 + '"C,1",2025-12,2026-01,100,R1,0\n',
    }
    for name, text in cases.items():
        r = import_text(client, text)
        check(f"拒绝：{name}", r.status_code == 400, f"status={r.status_code}")
    r = import_text(client, "claim_no,accident_month,payment_month,amount_cents,extra\n"
                            "C1,2025-12,2026-01,100,x\n")
    check("拒绝：无法识别的表头列", r.status_code == 400)
    r = import_text(client, HEADER + "C1,2025-12,2026-01\n")
    check("拒绝：列数不一致", r.status_code == 400)
    r = import_text(client, HEADER)
    check("拒绝：无数据行", r.status_code == 400)
    check("全部拒绝后 payments/batches 仍为空（原子性）",
          q(db, "SELECT COUNT(*) FROM payments")[0][0] == 0
          and q(db, "SELECT COUNT(*) FROM batches")[0][0] == 0)


# ---------------------------------------------------------------- 2b. 中文表头别名
def test_chinese_headers():
    print("\n[2b] 可选列中文别名：付款流水号 / 承接旧笔")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, "赔款号,事故月,支付月,金额\nZH1,2025-12,2025-12,5000\n")
    check("中文必需表头可导入", q(db, "SELECT amount_cents FROM payments WHERE claim_no='ZH1'")
          == [(5000,)])
    r = import_text(client, "赔款号,事故月,支付月,金额,付款流水号,承接旧笔\n"
                            "ZH1,2025-12,2025-12,5000,RZH,1\n")
    check("中文可选表头可承接旧笔", r.status_code == 200
          and "承接旧笔 1 笔" in r.get_data(as_text=True))
    r = import_text(client, "赔款号,事故月,支付月,金额,付款流水号,承接旧笔\n"
                            "ZH1,2025-12,2025-12,6000,RZH2,0\n")
    check("中文表头可报新流水", r.status_code == 200
          and q(db, "SELECT COUNT(*) FROM payments WHERE claim_no='ZH1'")[0][0] == 2)


# ---------------------------------------------------------------- 3. 覆盖语义 + 零额显示
def test_overwrite_and_zero():
    print("\n[3] 同流水覆盖（改事故月/支付月）与零额显示")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "A1,2025-12,2026-01,10000\nA2,2026-01,2026-01,5000\n")
    import_text(client, HEADER + "A1,2026-01,2026-01,30000\n")
    close_month(client, "2026-01")
    rows = q(db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-01'")
    check("封前同期覆盖：只按最新值归档 (2026-01,2026-01)=35000",
          rows == [("2026-01", "2026-01", 35000)], f"got {rows}")

    import_text(client, HEADER + "A1,2026-01,2026-02,30000\nA3,2026-01,2026-02,4000\n")
    close_month(client, "2026-02")
    check("跨期更正支付月：旧组合 -30000，新组合 +34000",
          archive_map(db, "2026-02") == {("2026-01", "2026-01"): -30000,
                                         ("2026-01", "2026-02"): 34000},
          f"got {archive_map(db, '2026-02')}")

    import_text(client, HEADER + "A5,2026-02,2026-03,9000\n")
    close_month(client, "2026-03")
    t = client.get("/triangle?start=2026-01&close=2026-03").get_data(as_text=True)
    check("已到月份零额显示 0", '<td class="num">0</td>' in t)
    check("累计值 5,000 / 39,000 / 9,000 正确",
          all(s in t for s in ("5,000", "39,000", "9,000")))


# ---------------------------------------------------------------- 4. 封账约束
def test_close_constraints():
    print("\n[4] 封账约束：只能封当前待结月，成功后进入次月")
    app, client, db = make_client()
    r = client.get("/close", follow_redirects=True)
    check("未设待结月时封账页引导设置", "请先设置首个待结月" in r.get_data(as_text=True))
    setup_pending(client)
    r = close_month(client, "2025-12")
    check("封过去月份被拒绝", "只能封当前待结月 2026-01" in r.get_data(as_text=True))
    r = close_month(client, "2026-01")
    check("空期间也可封账（0 组合）", "已封账 2026-01" in r.get_data(as_text=True))
    r = close_month(client, "2026-01")
    check("重复封同一月被拒绝", "只能封当前待结月 2026-02" in r.get_data(as_text=True))
    check("closes 表仅一条记录", len(q(db, "SELECT * FROM closes")) == 1)
    r = client.get("/triangle")
    check("空归档期三角页可访问", r.status_code == 200)


# ---------------------------------------------------------------- 5. 失败原子性
def test_batch_atomicity():
    print("\n[5] 整批原子性：部分行非法则整批不留痕")
    app, client, db = make_client()
    setup_pending(client)
    csv_text = HEADER + "G1,2025-12,2026-01,100\nG2,2025-12,2026-01,200\nG3,2026-02,2026-01,300\n"
    r = import_text(client, csv_text)
    check("含非法行的批次被拒绝", r.status_code == 400)
    check("前两行有效也未写入",
          q(db, "SELECT COUNT(*) FROM payments")[0][0] == 0
          and q(db, "SELECT COUNT(*) FROM batches")[0][0] == 0)


# ---------------------------------------------------------------- 6. 待结月锁定
def test_setup_locked_after_data():
    print("\n[6] 有数据后禁止改待结月")
    app, client, db = make_client()
    setup_pending(client, "2026-01")
    import_text(client, HEADER + "L1,2025-12,2026-01,100\n")
    r = setup_pending(client, "2026-03")
    check("修改被拒绝", "不能再修改待结月" in r.get_data(as_text=True))
    check("待结月保持 2026-01",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-01")


# ---------------------------------------------------------------- 7. 多流水分笔入账
def test_multiple_installments():
    print("\n[7] 同一赔款号多流水：分别入账、同流水只覆盖该笔")
    app, client, db = make_client()
    setup_pending(client)
    # 三条流水同批报送（新赔款号，从未有过旧笔）
    import_text(client, H6 +
                "M1,2025-11,2025-11,10000,P1,0\n"
                "M1,2025-11,2025-12,20000,P2,0\n"
                "M1,2025-12,2026-01,30000,P3,0\n")
    rows = q(db, "SELECT payment_ref, accident_month, payment_month, amount_cents"
                 " FROM payments WHERE claim_no='M1' ORDER BY payment_ref")
    check("M1 落三笔独立付款", len(rows) == 3, f"got {rows}")
    close_month(client, "2026-01")
    check("三笔分别归档到各自组合",
          archive_map(db, "2026-01") == {("2025-11", "2025-11"): 10000,
                                         ("2025-11", "2025-12"): 20000,
                                         ("2025-12", "2026-01"): 30000},
          f"got {archive_map(db, '2026-01')}")
    # 下期只重报 P2，改金额；其余两笔不受影响
    r = import_text(client, H6 + "M1,2025-11,2025-12,25000,P2,0\n")
    check("同流水重报成功", r.status_code == 200)
    check("只覆盖 P2，其他流水不动",
          q(db, "SELECT payment_ref, amount_cents FROM payments WHERE claim_no='M1' ORDER BY payment_ref")
          == [("P1", 10000), ("P2", 25000), ("P3", 30000)])
    check("重报结果计入覆盖重报", "覆盖重报 1 笔" in r.get_data(as_text=True))
    close_month(client, "2026-02")
    check("下期只为 P2 的差额 +5000 归档",
          archive_map(db, "2026-02") == {("2025-11", "2025-12"): 5000},
          f"got {archive_map(db, '2026-02')}")
    check("批次假设承接数为 0", q(db, "SELECT assumed_count FROM batches ORDER BY id") == [(0,), (0,)])


# ---------------------------------------------------------------- 8. 承接旧笔
def test_assume_legacy():
    print("\n[8] 承接旧笔：首次承接、无净额、无流水重报仍更正、不可改绑")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "K1,2025-11,2025-12,40000\nK2,2025-11,2025-12,9000\n")
    close_month(client, "2026-01")
    check("旧笔已归档", archive_map(db, "2026-01") ==
          {("2025-11", "2025-12"): 49000}, f"got {archive_map(db, '2026-01')}")
    legacy_id = q(db, "SELECT id FROM payments WHERE claim_no='K1' AND payment_ref IS NULL")[0][0]

    # 从未有过旧笔的 K9 也标记承接 -> 拒绝
    r = import_text(client, H6 + "K9,2025-11,2025-12,9000,R9A,1\n")
    check("拒绝：无旧笔却标记承接", r.status_code == 409
          and "没有无流水旧笔" in r.get_data(as_text=True))

    # 有旧笔但报流水却不承接 -> 拒绝
    r = import_text(client, H6 + "K1,2025-11,2025-12,40000,R1,0\n")
    check("拒绝：有旧笔但未指定承接", r.status_code == 409
          and "承接旧笔=1" in r.get_data(as_text=True))
    check("拒绝后旧笔未变",
          q(db, "SELECT payment_ref FROM payments WHERE id=?", (legacy_id,)) == [(None,)])

    # 恰好一条承接，只补流水号（金额月份不变）
    r = import_text(client, H6 + "K1,2025-11,2025-12,40000,R1,1\n")
    check("首次承接成功", r.status_code == 200 and "承接旧笔 1 笔" in r.get_data(as_text=True))
    row = q(db, "SELECT id, payment_ref, assumed_from_legacy FROM payments WHERE claim_no='K1'")
    check("承接复用同一条付款记录并打上标记", row == [(legacy_id, "R1", 1)], f"got {row}")
    check("承接批次 assumed_count=1",
          q(db, "SELECT assumed_count FROM batches ORDER BY id")[-1] == (1,))
    close_month(client, "2026-02")
    check("只补流水号不产生净额", archive_map(db, "2026-02") == {},
          f"got {archive_map(db, '2026-02')}")

    # 承接后无流水重报仍更正这笔
    r = import_text(client, HEADER + "K1,2025-11,2025-12,45000\n")
    check("承接后无流水重报成功", r.status_code == 200)
    check("无流水重报仍更新同一笔付款",
          q(db, "SELECT id, amount_cents FROM payments WHERE claim_no='K1'") == [(legacy_id, 45000)])
    # 新增另一条流水，独立入账
    r = import_text(client, H6 + "K1,2025-12,2026-03,7000,R2,0\n")
    check("承接后可新增其他流水", r.status_code == 200)
    close_month(client, "2026-03")
    check("封账按旧笔快照算净额 +5000，并为新流水 +7000",
          archive_map(db, "2026-03") == {("2025-11", "2025-12"): 5000,
                                         ("2025-12", "2026-03"): 7000},
          f"got {archive_map(db, '2026-03')}")

    # 承接关系不可改绑
    r = import_text(client, H6 + "K1,2025-11,2025-12,45000,R9,1\n")
    check("拒绝：承接关系不可改绑", r.status_code == 409
          and "不能改绑" in r.get_data(as_text=True))
    # 也不能用已属于其它付款的流水来承接（此时已无旧笔，先构造一个）
    import_text(client, HEADER + "K3,2026-01,2026-01,1000\n")
    import_text(client, H6 + "K3,2026-01,2026-01,1000,RK,1\n")
    r = import_text(client, H6 + "K3,2026-01,2026-01,1200,RK,1\n")
    check("拒绝：已承接的旧笔再次标记承接", r.status_code == 409
          and "再次标记承接" in r.get_data(as_text=True))
    r = import_text(client, H6 + "K3,2026-01,2026-01,1200,RK,0\n")
    check("承接流水不带标记可按普通覆盖更正", r.status_code == 200
          and q(db, "SELECT amount_cents FROM payments WHERE claim_no='K3'") == [(1200,)])
    r = import_text(client, H6 + "K3,2026-01,2026-01,1000,RY,1\n")
    check("拒绝：承接后再标记承接新流水", r.status_code == 409
          and "不能改绑" in r.get_data(as_text=True))
    check("拒绝后未改绑",
          q(db, "SELECT payment_ref FROM payments WHERE claim_no='K3'") == [("RK",)])
    # 已承接后再补一条同号新流水（不标记承接），独立入账
    r = import_text(client, H6 + "K1,2026-01,2026-02,3000,R7,0\n")
    check("承接后可新增其他流水", r.status_code == 200)
    check("新流水独立成笔",
          q(db, "SELECT payment_ref FROM payments WHERE claim_no='K1' ORDER BY payment_ref")
          == [("R1",), ("R2",), ("R7",)])


# ---------------------------------------------------------------- 8b. 承接时改月份/金额
def test_assume_moves_month():
    print("\n[8b] 承接时改支付月/金额：按旧笔上次封账快照算净额")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "S1,2025-11,2025-12,40000\n")
    close_month(client, "2026-01")            # 快照: 2025-11/2025-12 40000
    close_month(client, "2026-02")            # 空期，快照不变
    r = import_text(client, H6 + "S1,2025-11,2026-03,46000,PS1,1\n")
    check("承接行可同时更正金额与支付月", r.status_code == 200)
    close_month(client, "2026-03")
    check("净额 = 旧快照转出 -40000 + 新值 +46000",
          archive_map(db, "2026-03") == {("2025-11", "2025-12"): -40000,
                                         ("2025-11", "2026-03"): 46000},
          f"got {archive_map(db, '2026-03')}")
    t = client.get("/triangle?start=2025-11&close=2026-01").get_data(as_text=True)
    check("承接改期后，已结三角仍按原数回查", "40,000" in t)


# ---------------------------------------------------------------- 9. 无旧笔拒收无流水
def test_no_legacy_rejects_blank_ref():
    print("\n[9] 从未有过旧笔：报送流水后拒收无流水行（同批/跨批）")
    app, client, db = make_client()
    setup_pending(client)
    # 同批：新赔款号既有流水又有无流水行
    r = import_text(client, H6 +
                    "N1,2025-12,2026-01,100,R1,0\n"
                    "N1,2025-12,2026-01,200,,\n")
    check("拒绝：同批混填同号无流水行", r.status_code == 409
          and "从未有过无流水旧笔" in r.get_data(as_text=True))
    check("同批拒绝后整批未写入", q(db, "SELECT COUNT(*) FROM payments")[0][0] == 0)

    import_text(client, H6 + "N1,2025-12,2026-01,100,R1,0\n")
    r = import_text(client, HEADER + "N1,2025-12,2026-01,200\n")
    check("拒绝：跨批报送无流水行", r.status_code == 409
          and "从未有过无流水旧笔" in r.get_data(as_text=True))
    check("拒绝后无流水行未写入", q(db, "SELECT COUNT(*) FROM payments")[0][0] == 1)


# ---------------------------------------------------------------- 10. 同笔冲突整批退回
def test_same_target_conflict():
    print("\n[10] 两行最终指向同一笔付款：整批退回，与行序无关")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "P1,2025-11,2025-11,3000\n")
    close_month(client, "2026-01")
    # 一条流水承接旧笔 + 一条无流水行：同笔
    batch_a = H6 + "P1,2025-11,2025-11,3000,R1,1\nP1,2025-11,2025-11,3000,,\n"
    batch_b = H6 + "P1,2025-11,2025-11,3000,,\nP1,2025-11,2025-11,3000,R1,1\n"
    r1 = import_text(client, batch_a)
    r2 = import_text(client, batch_b)
    check("冲突批次 A 整批退回", r1.status_code == 409
          and "同一笔付款" in r1.get_data(as_text=True))
    check("冲突批次 B（行序相反）同样整批退回", r2.status_code == 409
          and "同一笔付款" in r2.get_data(as_text=True))
    check("两种行序结果一致（仅行号不同）",
          r1.get_data(as_text=True).count("整批退回") == r2.get_data(as_text=True).count("整批退回"))
    check("冲突未改写库中数据",
          q(db, "SELECT payment_ref FROM payments WHERE claim_no='P1'") == [(None,)]
          and len(q(db, "SELECT * FROM batches")) == 1)

    # 同一赔款号两条承接行 -> 退回
    r = import_text(client, H6 +
                    "P1,2025-11,2025-11,3000,R1,1\n"
                    "P1,2025-11,2025-11,3000,R2,1\n")
    check("拒绝：同批两条承接行", r.status_code == 409
          and "承接须恰好一条" in r.get_data(as_text=True))
    check("冲突后旧笔仍未承接",
          q(db, "SELECT payment_ref FROM payments WHERE claim_no='P1'") == [(None,)])

    # 一个批次混填有流水与无流水的不同付款：允许
    import_text(client, HEADER + "P2,2025-11,2025-11,1000\nP3,2025-12,2025-12,2000\n")
    r = import_text(client, H6 +
                    "P2,2025-11,2025-11,1000,R2,1\n"
                    "P3,2025-12,2025-12,2000,,\n"
                    "P4,2026-01,2026-01,500,R4,0\n")
    check("允许：同批混填不同付款", r.status_code == 200)

    # 同一赔款号同批：承接旧笔 + 另一条全新流水，允许
    import_text(client, HEADER + "P5,2025-11,2025-11,700\n")
    close_month(client, "2026-02")
    r = import_text(client, H6 +
                    "P5,2025-11,2025-11,700,R5A,1\n"
                    "P5,2026-01,2026-02,300,R5B,0\n")
    check("允许：同批承接旧笔并同时报送另一条新流水", r.status_code == 200,
          r.get_data(as_text=True)[:300])
    check("承接与新流水各自成笔",
          q(db, "SELECT payment_ref, amount_cents FROM payments WHERE claim_no='P5'"
                " ORDER BY payment_ref") == [("R5A", 700), ("R5B", 300)])


# ---------------------------------------------------------------- 11. 冲突时承接不落库
def test_assumption_rollback():
    print("\n[11] 承接与整批入账同事务：后行冲突则承接也不落库")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "Q1,2025-11,2025-11,1000\n")
    close_month(client, "2026-01")
    r = import_text(client, H6 +
                    "Q1,2025-11,2025-11,1000,R1,1\n"
                    "Q9,2026-02,2026-01,100,X,0\n")  # 第二节非法：支付月早于事故月
    check("含非法行的承接批次被拒", r.status_code == 400)
    check("承接未落库",
          q(db, "SELECT payment_ref FROM payments WHERE claim_no='Q1'") == [(None,)])
    check("未新增付款/批次",
          len(q(db, "SELECT * FROM payments")) == 1
          and len(q(db, "SELECT * FROM batches")) == 1)


# ---------------------------------------------------------------- 12. 旧库迁移
def test_legacy_db_migration():
    print("\n[12] 旧库直接接着用：启动自动迁移，已结快照沿用")
    tmp = tempfile.mkdtemp(prefix="lt-mig-")
    db_path = os.path.join(tmp, "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE batches (id INTEGER PRIMARY KEY AUTOINCREMENT, period_month TEXT NOT NULL,
      row_count INTEGER NOT NULL, total_cents INTEGER NOT NULL,
      overwritten INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')));
    CREATE TABLE claims (claim_no TEXT PRIMARY KEY, accident_month TEXT NOT NULL,
      payment_month TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
      batch_id INTEGER NOT NULL REFERENCES batches(id));
    CREATE TABLE claim_archived (claim_no TEXT PRIMARY KEY, accident_month TEXT NOT NULL,
      payment_month TEXT NOT NULL, amount_cents INTEGER NOT NULL);
    CREATE TABLE closes (close_month TEXT PRIMARY KEY, batch_count INTEGER NOT NULL,
      combo_count INTEGER NOT NULL,
      closed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')));
    CREATE TABLE archive (close_month TEXT NOT NULL, accident_month TEXT NOT NULL,
      payment_month TEXT NOT NULL, net_cents INTEGER NOT NULL,
      PRIMARY KEY (close_month, accident_month, payment_month));
    INSERT INTO meta VALUES ('pending_month', '2026-02');
    INSERT INTO batches(id, period_month, row_count, total_cents)
      VALUES (1, '2026-01', 1, 50000);
    INSERT INTO closes(close_month, batch_count, combo_count) VALUES ('2026-01', 1, 1);
    INSERT INTO archive VALUES ('2026-01', '2025-12', '2025-12', 50000);
    INSERT INTO claims VALUES ('OLD1', '2025-12', '2025-12', 50000, 1);
    INSERT INTO claim_archived VALUES ('OLD1', '2025-12', '2025-12', 50000);
    """)
    conn.commit()
    conn.close()

    app = create_app({"DB_PATH": db_path, "TESTING": True})
    client = app.test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        p = conn.execute("SELECT * FROM payments").fetchall()
        a = conn.execute("SELECT * FROM payment_archived").fetchall()
        check("旧赔案转为无流水旧笔", len(p) == 1 and p[0]["claim_no"] == "OLD1"
              and p[0]["payment_ref"] is None)
        check("归档快照挂到新付款 id 上", len(a) == 1 and a[0]["payment_id"] == p[0]["id"])
        check("旧表已清理", conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('claims','claim_archived')"
        ).fetchall() == [])
        check("待结月与已结月保留", conn.execute(
            "SELECT value FROM meta WHERE key='pending_month'").fetchone()[0] == "2026-02"
            and conn.execute("SELECT close_month FROM closes").fetchone()[0] == "2026-01")
    finally:
        conn.close()

    # 旧库接着用：承接旧笔只补流水号，不产生净额；旧三角保持原样
    r = client.post("/import", data={"csv_text":
                    H6 + "OLD1,2025-12,2025-12,50000,R1,1\n"})
    check("旧库承接旧笔成功", r.status_code == 200)
    r = close_month(client, "2026-02")
    check("旧库封账推进", "已封账 2026-02" in r.get_data(as_text=True))
    check("承接不产生净额", archive_map(db_path, "2026-02") == {},
          f"got {archive_map(db_path, '2026-02')}")
    t = client.get("/triangle?start=2025-12&close=2026-01").get_data(as_text=True)
    check("迁移后已结三角按原数回查", "50,000" in t)

    # 重启（再次 init）不重复迁移、不重复入账
    create_app({"DB_PATH": db_path, "TESTING": True})
    check("重复启动不产生重复付款",
          q(db_path, "SELECT COUNT(*) FROM payments")[0][0] == 1)


# ---------------------------------------------------------------- 13. 导入/封账并发
def test_concurrent_import_and_close():
    print("\n[13] 并发：导入与封账重叠时整批计入本期或下期")
    app, client, db = make_client()
    setup_pending(client)
    barrier = threading.Barrier(4)
    results, errors = [], []

    def worker(i):
        c = app.test_client()
        csv_text = H6 + f"T{i},2025-12,2026-01,{1000 * (i + 1)},R{i},0\n"
        try:
            barrier.wait(timeout=10)
            time.sleep(0.01 * i)
            r = c.post("/import", data={"csv_text": csv_text})
            results.append((i, r.status_code))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"worker{i}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    barrier.wait(timeout=10)
    time.sleep(0.02)  # 让部分导入先提交，制造重叠
    r1 = close_month(client, "2026-01")
    for t in threads:
        t.join()
    r2 = close_month(client, "2026-02")

    check("并发期间无异常", not errors, "; ".join(errors))
    check("3 个批次全部成功", sorted(s for _, s in results) == [200, 200, 200],
          f"got {results}")
    check("两次封账均成功", "已封账 2026-01" in r1.get_data(as_text=True)
          and "已封账 2026-02" in r2.get_data(as_text=True))
    periods = [row[0] for row in q(db, "SELECT period_month FROM batches")]
    check("每批计入 2026-01 或 2026-02", set(periods) <= {"2026-01", "2026-02"}
          and len(periods) == 3, f"got {periods}")
    total_payments = q(db, "SELECT SUM(amount_cents) FROM payments")[0][0]
    total_archived = q(db, "SELECT SUM(net_cents) FROM archive")[0][0]
    check("两期归档净额合计 == 付款合计（无丢失无重复）",
          total_archived == total_payments == 6000,
          f"archived={total_archived} payments={total_payments}")


def main():
    for fn in (test_full_flow_two_periods, test_demo_installment_batch,
               test_import_validation,
               test_chinese_headers,
               test_overwrite_and_zero, test_close_constraints,
               test_batch_atomicity, test_setup_locked_after_data,
               test_multiple_installments, test_assume_legacy,
               test_assume_moves_month,
               test_no_legacy_rejects_blank_ref, test_same_target_conflict,
               test_assumption_rollback, test_legacy_db_migration,
               test_concurrent_import_and_close):
        fn()
    print(f"\n==== 结果：{len(_passed)} 通过，{len(_failed)} 失败 ====")
    if _failed:
        print("失败项：")
        for name in _failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
