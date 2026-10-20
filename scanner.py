# -*- coding: utf-8 -*-
"""
=====================================================================
数据管线 —— 负责"把数据搞到手"，不负责"怎么选"
=====================================================================

流程：
    1. 判断是不是交易日（周末/节假日直接退出）
    2. 拉全市场快照（新浪，5400+ 只，含名称/现价/涨跌幅）
    3. 基础过滤（ST、退市、北交所、低价股）
    4. 并发拉日线 + 交给 strategy.evaluate() 判断
    5. 维护横盘池、分类信号、写 result.json

改选股逻辑请改 strategy.py，不要改这里。

运行时可选环境变量：
    SCAN_SCOPE   all=全市场扫描（默认） | hot=只扫涨幅榜（省额度）
    WORKERS      并发线程数，默认 20
    SCAN_LIMIT   只扫前 N 只，仅用于本地调试
=====================================================================
"""
import warnings
warnings.filterwarnings("ignore")

import os
import re
import json
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import akshare as ak

import strategy

CFG = {
    "workers": int(os.environ.get("WORKERS", "20")),
    "scan_scope": os.environ.get("SCAN_SCOPE", "all").strip().lower(),  # all | hot
    "scan_chg_min": 2.0,      # hot 模式下的涨幅门槛
    "max_scan_hot": 900,      # hot 模式下涨幅榜上限
    "max_out": 3,             # 强/弱信号各最多展示几只
    "max_watch": 20,          # 观察池最多展示几只
    "pool_file": "pool.json",
    "result_file": "result.json",
    "fetch_retry": 2,
    "fetch_sleep": 0.05,      # 每只之间的间隔，纯为减轻新浪压力
}
BAD = re.compile(r"ST|退")


def log(m):
    print(m, flush=True)


# ---------------------------------------------------------------- 交易日
def is_trading_day(d=None):
    """判断是否 A 股交易日（含调休）。日历拉不到时保守按交易日处理。"""
    d = d or datetime.date.today()
    try:
        cal = ak.tool_trade_date_hist_sina()
        days = set(pd.to_datetime(cal["trade_date"]).dt.date.tolist())
        return d in days
    except Exception as e:
        log(f"  交易日历获取失败({type(e).__name__})，按交易日处理")
        return True


# ---------------------------------------------------------------- 快照
def load_spot():
    for i in range(3):
        try:
            df = ak.stock_zh_a_spot()
            if df is not None and len(df) > 1000:
                df.columns = [str(c).strip() for c in df.columns]
                return df
        except Exception as e:
            log(f"  快照重试{i + 1}: {e}")
            time.sleep(3)
    raise RuntimeError("快照获取失败")


# ---------------------------------------------------------------- 单只日线
def fetch(code):
    for _ in range(CFG["fetch_retry"]):
        try:
            h = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
            if h is None or len(h) == 0:
                return None
            return h
        except Exception:
            time.sleep(0.8)
    return None


def calc_mktcap(h):
    """流通市值(亿) = 流通股本 × 收盘价。不依赖东财接口。"""
    try:
        last = h.iloc[-1]
        shares = float(last.get("outstanding_share") or 0)
        price = float(last["close"])
        if shares > 0 and price > 0:
            return shares * price / 1e8
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 单只处理
def work(code, name, prev):
    """在子线程里跑：抓日线 → 算市值 → 交给策略判断。"""
    try:
        h = fetch(code)
        if h is None:
            return None
        if CFG["fetch_sleep"]:
            time.sleep(CFG["fetch_sleep"])
        return strategy.evaluate(h, code, name, prev, calc_mktcap(h))
    except Exception:
        return None


# ---------------------------------------------------------------- 池子
def load_pool():
    if not os.path.exists(CFG["pool_file"]):
        return {}
    try:
        with open(CFG["pool_file"], encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 主流程
def main():
    t0 = time.time()
    today = datetime.date.today().isoformat()

    if not is_trading_day():
        log(f"今天({today})不是A股交易日，跳过")
        save_json(CFG["result_file"], {
            "date": today, "skip": True, "reason": "非交易日",
            "scanned": 0, "pool_size": 0,
            "strategy": {"name": strategy.NAME, "version": strategy.VERSION,
                         "rules": strategy.describe()},
            "strong": [], "weak": [], "watchlist": [],
        })
        return

    # ---- 1. 快照 + 基础过滤 ----
    log("1/5 全市场快照")
    spot = load_spot()
    log(f"  {len(spot)} 只")

    df = spot.copy()
    df = df[~df["名称"].astype(str).str.contains(BAD)]
    df = df[~df["代码"].astype(str).map(strategy.is_excluded)]
    df["最新价"] = pd.to_numeric(df["最新价"], errors="coerce")
    df["涨跌幅"] = pd.to_numeric(df.get("涨跌幅"), errors="coerce")
    df = df.dropna(subset=["最新价"])
    df = df[df["最新价"] >= strategy.CFG["price_min"]]
    df = df.drop_duplicates(subset=["代码"])
    log(f"2/5 基础过滤后 {len(df)} 只")

    names = df.set_index("代码")["名称"].to_dict()

    # ---- 2. 决定扫描范围 ----
    pool = load_pool()
    all_codes = list(names.keys())
    if CFG["scan_scope"] == "hot":
        hot = df[df["涨跌幅"] >= CFG["scan_chg_min"]]
        hot_codes = list(hot.sort_values("涨跌幅", ascending=False)["代码"])
        hot_codes = hot_codes[: CFG["max_scan_hot"]]
        scan_codes = list(dict.fromkeys(list(pool.keys()) + hot_codes))
        scan_codes = [c for c in scan_codes if c in names]
        log(f"  模式=hot：涨幅榜 {len(hot_codes)} + 池内 {len(pool)}")
    else:
        scan_codes = all_codes
        log("  模式=all：全市场扫描")

    limit = int(os.environ.get("SCAN_LIMIT", "0") or 0)
    if limit:
        scan_codes = scan_codes[:limit]
        log(f"  【调试】只扫前 {limit} 只")

    # ---- 3. 并发抓取 + 判断 ----
    log(f"3/5 扫描 {len(scan_codes)} 只（{CFG['workers']} 线程并发）")
    records, fail, done = {}, 0, 0
    aborted = False
    ex = ThreadPoolExecutor(max_workers=CFG["workers"])
    futs = {ex.submit(work, c, names.get(c, ""), pool.get(c)): c for c in scan_codes}
    try:
        for f in as_completed(futs):
            done += 1
            code = futs[f]
            try:
                rec = f.result()
            except Exception:
                rec = None
            if rec is None:
                fail += 1
            else:
                rec["code"] = code
                rec["name"] = names.get(code, "")
                records[code] = rec
            if done % 300 == 0:
                log(f"  {done}/{len(scan_codes)} 通过{len(records)} 失败{fail} "
                    f"({time.time() - t0:.0f}s)")
            # 熔断：前 200 只几乎全失败，说明接口被限流，别再白跑半小时
            if done == 200 and len(records) < 10:
                log("  ⚠️ 前 200 只几乎全部失败，疑似被限流，中止本次扫描")
                aborted = True
                break
    finally:
        # 不等剩下的任务：中止时立刻返回，别把 40 分钟额度耗光
        ex.shutdown(wait=False, cancel_futures=True)

    if aborted:
        save_json(CFG["result_file"], {
            "date": today, "skip": True, "reason": "扫描被中止（数据源异常）",
            "scanned": done, "pool_size": len(pool),
            "strategy": {"name": strategy.NAME, "version": strategy.VERSION,
                         "rules": strategy.describe()},
            "strong": [], "weak": [], "watchlist": [],
        })
        log("  已写 result.json（标记为跳过），池子文件保持不变")
        return

    # ---- 4. 建池 + 分类 ----
    new_pool, strong, weak = {}, [], []
    for code, rec in records.items():
        m = rec["metrics"]
        new_pool[code] = {
            "first_seen": (pool.get(code) or {}).get("first_seen", today),
            "days": m["days"],
            "name": rec["name"],
            "amp": m["amp"],
            "last": today,
            "pos": m["pos"],
            "close": m["close"],
            "box_high": m["box_high"],
            "box_low": m["box_low"],
        }
        if rec["level"] == "强":
            strong.append(rec)
        elif rec["level"] == "弱":
            weak.append(rec)

    save_json(CFG["pool_file"], new_pool)

    strong.sort(key=lambda x: -x["score"])
    weak.sort(key=lambda x: -x["score"])

    trig = {r["code"] for r in strong + weak}
    watch = sorted(
        [(c, v) for c, v in new_pool.items() if c not in trig],
        key=lambda x: (-(x[1].get("pos") or 0), -x[1]["days"]),
    )

    res = {
        "date": today,
        "scanned": len(scan_codes),
        "scanned_ok": len(records),
        "scope": CFG["scan_scope"],
        "pool_size": len(new_pool),
        "strategy": {"name": strategy.NAME, "version": strategy.VERSION,
                     "rules": strategy.describe()},
        "strong": strong[: CFG["max_out"]],
        "weak": weak[: CFG["max_out"]],
        "strong_total": len(strong),
        "weak_total": len(weak),
        "watchlist": [
            {
                "code": c, "name": v["name"], "days": v["days"], "amp": v["amp"],
                "first_seen": v["first_seen"], "pos": v.get("pos"),
                "close": v.get("close"), "box_high": v.get("box_high"),
                "gap_to_top": round((v["box_high"] / v["close"] - 1) * 100, 1)
                if v.get("close") and v.get("box_high") else None,
            }
            for c, v in watch[: CFG["max_watch"]]
        ],
    }
    save_json(CFG["result_file"], res)
    log(f"4/5 完成：池 {len(new_pool)} 强 {len(strong)} 弱 {len(weak)} "
        f"耗时 {time.time() - t0:.0f}s")
    log("5/5 已写 result.json")


if __name__ == "__main__":
    main()
