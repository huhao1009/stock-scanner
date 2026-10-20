# -*- coding: utf-8 -*-
"""
离线自测：不联网、不依赖 akshare，用造出来的 K 线验证策略层和报告层。

跑法：
    python selftest.py

构造「缓跌 → 急跌 → 底部企稳 → 收阳小反弹」的合成形态（该形态下
EMA5 恰好在最后一天上穿趋势线，与真实信号日 J 中位 38.7 的形态一致），
检查 strategy.evaluate() 的判断是否符合预期，再把结果渲染成
selftest_report.html 人工核对版式。
换策略之后跑一遍，能立刻知道新逻辑有没有跑通。

注意：反例用例（J过热/回调不足/收阴）在扰动单一条件时可能连带破坏
金叉条件，因此它们断言的是「这种行情下绝不能出信号」，是联合防线
而非单因子隔离测试。
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy

PASS, FAIL = "✅ PASS", "❌ FAIL"

# 基准形态参数（随机搜索得到，可稳定触发金叉 + J<50 + 买回调 + 收阳）
BASE_P = dict(g_days=38, g_tot=5.009893646721389, s_days=6,
              s_pct=2.911018305577632, d_days=16, d_pct=-0.27568367172295394,
              dip=2.5, last_pct=3.2177715498274386)


def make_hist(g_days, g_tot, s_days, s_pct, d_days, d_pct, dip, last_pct,
              base=10.0):
    """缓跌 g_days 天共跌 g_tot% → 急跌 s_days×s_pct% → 企稳 d_days 天
    →（可选）回抽 dip% → 最后一天收阳涨 last_pct%。"""
    rows = []
    for _ in range(80):
        rows.append((base, base, base, base, 1e6))
    pr = base
    for i in range(g_days):
        pr = base - g_tot * (i + 1) / g_days
        rows.append((pr + .01, pr, pr - .01, pr, 1e6))
    for _ in range(s_days):
        pr = pr * (1 - s_pct / 100)
        rows.append((pr * 1.003, pr * 1.006, pr * .996, pr, 1.3e6))
    for _ in range(d_days):
        pr = pr * (1 + d_pct / 100)
        rows.append((pr * .999, pr * 1.004, pr * .996, pr, 9e5))
    if dip:
        pr = pr * (1 - dip / 100)
        rows.append((pr * 1.002, pr * 1.004, pr * .996, pr, 1.0e6))
    o = pr * .998
    c2 = pr * (1 + last_pct / 100)
    rows.append((o, c2 + .02, o * .997, c2, 1.2e6))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def main():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"[{PASS if cond else FAIL}] {name} {detail}")
        if not cond:
            ok = False

    # ---------- 触发用例 ----------
    strategy.set_market(0.6)
    hist = make_hist(**BASE_P)
    rec = strategy.evaluate(hist, "sh600000", "测试股", None, 50.0)

    check("基准形态应触发信号", rec is not None)
    if rec:
        check("信号 level=弱（推送与否由密度门槛决定）", rec["level"] == "弱")
        check("fields 含持有建议", any(f[0] == "持有建议" for f in rec["fields"]))
        m = rec["metrics"]
        check("metrics 含 sleep 且在 0~120", 0 <= m["sleep"] <= 120,
              f"sleep={m['sleep']}")
        check("score 在 0~100", 0 <= rec["score"] <= 100, f"score={rec['score']}")
        check("summary 非空", bool(rec["summary"]))
        check("why 非空", bool(rec["why"]))

    # ---------- 大盘宽度 ----------
    strategy.set_market(0.4)
    check("大盘宽度 40% 不足 → 无信号",
          strategy.evaluate(hist, "sh600000", "测试股", None, 50.0) is None)
    strategy.set_market(None)
    check("宽度未知 → 无信号（保守处理）",
          strategy.evaluate(hist, "sh600000", "测试股", None, 50.0) is None)
    strategy.set_market(0.6)

    # ---------- 市值 / 数据长度 ----------
    check("市值超上限 → 无信号",
          strategy.evaluate(hist, "sh600000", "测试股", None, 500.0) is None)
    check("市值低于下限 → 无信号",
          strategy.evaluate(hist, "sh600000", "测试股", None, 5.0) is None)
    check("K线不足 130 根 → 无信号",
          strategy.evaluate(hist.head(120), "sh600000", "测试股", None, 50.0)
          is None)

    # ---------- 过滤条件反例（联合防线，见文件头说明） ----------
    p = dict(BASE_P, last_pct=-2.0)
    check("收阴日 → 无信号",
          strategy.evaluate(make_hist(**p), "sh600000", "测试股", None, 50.0)
          is None)
    p = dict(BASE_P, d_pct=0.5, last_pct=6.0)
    check("反弹过强 J 过热 → 无信号",
          strategy.evaluate(make_hist(**p), "sh600000", "测试股", None, 50.0)
          is None)
    p = dict(BASE_P, s_pct=0.8, dip=0)
    check("跌得不够深（回调不足）→ 无信号",
          strategy.evaluate(make_hist(**p), "sh600000", "测试股", None, 50.0)
          is None)

    # ---------- 板块过滤 ----------
    cases = [
        ("sh688001", True, "科创板"), ("sh689009", True, "科创板CDR"),
        ("sz300466", True, "创业板"), ("sz301536", True, "创业板"),
        ("bj430047", True, "北交所"), ("sh900901", True, "B股"),
        ("sh600519", False, "沪主板"), ("sz000001", False, "深主板"),
    ]
    for code, want, tag in cases:
        got = strategy.is_excluded(code)
        check(f"板块过滤 {tag} {code} → {'剔除' if want else '保留'}",
              got == want)

    # ---------- describe ----------
    rules = strategy.describe()
    check("describe() 返回规则说明", isinstance(rules, list) and len(rules) >= 3)

    # ---------- 报告渲染（用触发信号 + 伪造密度达标/未达标两种结果） ----------
    from report import build, card
    try:
        good = ({"code": "sh600000", "name": "测试股", **rec}
                if rec else None)
        res_ok = {
            "date": "2026-10-20", "scanned": 2479, "scanned_ok": 2479,
            "scope": "all", "breadth": 63.0,
            "strategy": {"name": strategy.NAME, "version": strategy.VERSION,
                         "rules": rules},
            "signal_total": 24, "density_threshold": 17, "density_ok": True,
            "strong": [good] if good else [],
            "weak": [],
            "pool_history": [
                {"code": "sh600000", "name": "测试股", "last_date": "2026-10-20",
                 "close": 4.02, "sleep": 0, "J": 39.4},
                {"code": "sz000002", "name": "历史信号", "last_date": "2026-10-13",
                 "close": 8.88, "sleep": 5, "J": 31.0},
            ],
        }
        html = build(res_ok)
        check("报告渲染（密度达标）含出票数", "出票 24 只" in html)
        check("报告渲染含持有建议", "20 个交易日" in html)
        if good:
            check("报告渲染信号卡", card(good, 1) and "测试股" in card(good, 1))

        res_no = dict(res_ok, signal_total=3, density_ok=False,
                      strong=[], weak=res_ok["strong"])
        html2 = build(res_no)
        check("报告渲染（未达标）提示不出手", "仅记录" in html2)

        with open("selftest_report.html", "w", encoding="utf-8") as f:
            f.write(html)
        with open("selftest_result.json", "w", encoding="utf-8") as f:
            json.dump(res_ok, f, ensure_ascii=False, indent=1)
    except Exception as e:
        check(f"报告渲染异常: {e}", False)

    print()
    print("全部通过 ✅" if ok else "存在失败项 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
