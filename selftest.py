# -*- coding: utf-8 -*-
"""
离线自测：不联网、不依赖 akshare，用造出来的 K 线验证策略层和报告层。

跑法：
    python selftest.py

它会构造 5 种典型形态，检查 strategy.evaluate() 的判断是否符合预期，
然后把结果渲染成 selftest_report.html，人工看一眼版式对不对。
换策略之后跑一遍，能立刻知道新逻辑有没有跑通。
"""
import json
import sys

import pandas as pd

import strategy
import report as rpt


def make_hist(base=10.0, amp=0.05, n=120, last_vol_x=1.0, last_chg=0.0,
              days_flat=90):
    """
    造一段日线：
      - 前 n-days_flat 天随便动
      - 后 days_flat 天在一个窄箱子里横着（振幅约 amp）
      - 最后一天按 last_vol_x 放量、按 last_chg 涨跌
    """
    rows = []
    for i in range(n):
        rows.append({
            "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
            "open": base, "high": base * 1.005, "low": base * 0.995,
            "close": base, "volume": 1e6, "amount": 1e7,
            "outstanding_share": 3e8, "turnover": 0.01,
        })
    df = pd.DataFrame(rows)
    # 横盘段：在 base*(1±amp/2) 之间小幅波动
    flat_start = n - days_flat - 1
    for i in range(flat_start, n - 1):
        k = (i - flat_start) % 10
        p = base * (1 + amp * (k / 9.0 - 0.5))
        df.loc[i, ["open", "close"]] = p
        df.loc[i, "high"] = p * 1.002
        df.loc[i, "low"] = p * 0.998
        df.loc[i, "volume"] = 1e6 * (0.8 + 0.4 * (k / 9.0))
    # 让横盘段的最后一天停在箱体中部，否则"昨天就在箱顶"，算出来的箱位会失真
    mid = n - 2
    df.loc[mid, ["open", "close"]] = base
    df.loc[mid, "high"] = base * 1.002
    df.loc[mid, "low"] = base * 0.998
    # 最后一天
    prev = df.loc[n - 2, "close"]
    p = prev * (1 + last_chg)
    df.loc[n - 1, ["open", "close"]] = p
    df.loc[n - 1, "high"] = p * 1.002
    df.loc[n - 1, "low"] = p * 0.998
    df.loc[n - 1, "volume"] = 1e6 * last_vol_x
    return df


CASES = [
    # (标题, 造数据参数, 期望 level)
    # amp=0.25 造出来的箱体振幅约 25%，贴近真实标的
    ("横盘+今日放量涨10%（强信号，贴箱顶）",
     dict(amp=0.25, last_vol_x=5.0, last_chg=0.10), "强"),
    ("横盘+今日小放量涨3%（弱信号）",
     dict(amp=0.25, last_vol_x=1.8, last_chg=0.03), "弱"),
    ("横盘+今日没动（只进池）",
     dict(amp=0.25, last_vol_x=1.0, last_chg=0.002), None),
    ("横盘+今日暴涨19%（已冲出箱顶，追高预警）",
     dict(amp=0.25, last_vol_x=3.0, last_chg=0.19), "强"),
    ("根本不在横盘（振幅过大）",
     dict(amp=0.80, last_vol_x=3.0, last_chg=0.05), None),
]

MKT_OK = 30.0
MKT_BIG = 800.0


def main():
    ok = True
    records = []
    for title, kw, expect in CASES:
        h = make_hist(**kw)
        rec = strategy.evaluate(h, "sz000001", title, None, MKT_OK)
        got = rec["level"] if rec else None
        if rec:
            rec["code"] = "sz000001"
            rec["name"] = title
        flag = "PASS" if got == expect else "FAIL"
        if flag == "FAIL":
            ok = False
        print(f"[{flag}] {title}: 期望 {expect} / 实际 {got}")
        if rec:
            print(f"        summary: {rec['summary']}")
            print(f"        box: {rec['metrics']['box_low']}~{rec['metrics']['box_high']}"
                  f" pos={rec['metrics']['pos']}% score={rec['score']}")
            records.append(rec)

    # 市值超上限 → 必须被剔除
    h = make_hist(amp=0.05, last_vol_x=5.0, last_chg=0.06)
    big = strategy.evaluate(h, "sh600519", "市值超限", None, MKT_BIG)
    print(f"[{'PASS' if big is None else 'FAIL'}] 市值超上限应被剔除: {big}")
    if big is not None:
        ok = False

    # 报告渲染
    res = {
        "date": "2026-10-20", "scanned": 5000, "scanned_ok": 4800,
        "scope": "all", "pool_size": len(records),
        "strategy": {"name": strategy.NAME, "version": strategy.VERSION,
                     "rules": strategy.describe()},
        "strong": [r for r in records if r["level"] == "强"][:3],
        "weak": [r for r in records if r["level"] == "弱"][:3],
        "strong_total": 2, "weak_total": 1,
        "watchlist": [
            {"code": r["code"], "name": r["name"], "days": r["metrics"]["days"],
             "amp": r["metrics"]["amp"], "pos": r["metrics"]["pos"],
             "close": r["metrics"]["close"], "box_high": r["metrics"]["box_high"],
             "gap_to_top": r["metrics"]["gap_to_top"]}
            for r in records
        ],
    }
    html = rpt.build(res)
    with open("selftest_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n报告已生成 selftest_report.html（{len(html)} 字节）")
    with open("selftest_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    print("\n全部通过 ✅" if ok else "\n有失败项 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
