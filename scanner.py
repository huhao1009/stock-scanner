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
    "max_show": 30,           # 报告里最多展示几张信号卡
    "push_names": 12,         # 微信推送最多列几只名字
    # ---- 密度门槛（回测结论：当日出票稀疏时信号无超额，不推） ----
    # 达标 = 当日信号数 >= max(下限, 比例×有效扫描数)
    "density_floor": 7,       # 绝对下限
    "density_ratio": 0.007,   # 约 0.7%（回测：1000只样本出票7只起才有超额）
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
    """在子线程里跑：抓日线 → 算市值 → 交给策略判断。
    返回 (是否成功抓到日线, 信号或None)。抓取失败与"今天没信号"必须区分开，
    否则密度门槛的分母（有效扫描数）会算错。"""
    try:
        h = fetch(code)
        if h is None:
            return False, None
        if CFG["fetch_sleep"]:
            time.sleep(CFG["fetch_sleep"])
        return True, strategy.evaluate(h, code, name, prev, calc_mktcap(h))
    except Exception:
        return False, None


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

    # 大盘宽度（涨家数占比）注入策略层：这是配方里的市场环境条件
    chg_col = pd.to_numeric(df.get("涨跌幅"), errors="coerce").dropna()
    breadth = float((chg_col > 0).mean()) if len(chg_col) else None
    strategy.set_market(breadth)
    log(f"  大盘涨家数占比 {breadth*100:.1f}%" if breadth is not None
        else "  大盘宽度无法计算，今天不会出信号")

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
    records, fail, fetched, done = {}, 0, 0, 0
    aborted = False
    ex = ThreadPoolExecutor(max_workers=CFG["workers"])
    futs = {ex.submit(work, c, names.get(c, ""), pool.get(c)): c for c in scan_codes}
    try:
        for f in as_completed(futs):
            done += 1
            code = futs[f]
            try:
                ok, rec = f.result()
            except Exception:
                ok, rec = False, None
            if not ok:
                fail += 1
            else:
                fetched += 1
                if rec is not None:
                    rec["code"] = code
                    rec["name"] = names.get(code, "")
                    records[code] = rec
            if done % 300 == 0:
                log(f"  {done}/{len(scan_codes)} 抓到{fetched} 信号{len(records)} "
                    f"失败{fail} ({time.time() - t0:.0f}s)")
            # 熔断：前 200 只几乎全失败，说明接口被限流，别再白跑半小时
            if done == 200 and fetched < 10:
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

    # ---- 4. 信号聚合 + 密度门槛 ----
    signals = list(records.values())
    # 排序：休眠短的在前（连板特征），同分按 score
    signals.sort(key=lambda r: (r["metrics"]["sleep"], -r["score"]))

    # 信号历史池：最近 14 天出过信号的票都留着，供回顾排查
    new_pool = {
        r["code"]: {"name": r["name"], "last_date": today,
                    "close": r["metrics"]["close"],
                    "sleep": r["metrics"]["sleep"], "J": r["metrics"]["J"]}
        for r in signals
    }
    keep_from = (datetime.date.today()
                 - datetime.timedelta(days=14)).isoformat()
    for c, v in pool.items():
        if c not in new_pool and str(v.get("last_date", "")) >= keep_from:
            new_pool[c] = v
    save_json(CFG["pool_file"], new_pool)

    # 密度门槛：回测结论 —— 当日出票稀疏（<7只/千只）时信号无超额，不推送
    threshold = max(CFG["density_floor"],
                    round(CFG["density_ratio"] * max(fetched, 1)))
    density_ok = len(signals) >= threshold
    if density_ok:
        strong = [dict(r, level="强") for r in signals]
        weak = []
        log(f"  ✅ 密度达标：出票 {len(signals)} ≥ 门槛 {threshold}，推送")
    else:
        strong, weak = [], signals
        log(f"  ⛔ 密度未达标：出票 {len(signals)} < 门槛 {threshold}，仅记录不推送")

    res = {
        "date": today,
        "scanned": len(scan_codes),
        "scanned_ok": fetched,
        "scope": CFG["scan_scope"],
        "breadth": round(breadth * 100, 1) if breadth is not None else None,
        "pool_size": len(new_pool),
        "strategy": {"name": strategy.NAME, "version": strategy.VERSION,
                     "rules": strategy.describe()},
        "signal_total": len(signals),
        "density_threshold": threshold,
        "density_ok": density_ok,
        "strong": strong[: CFG["max_show"]],
        "weak": weak[: CFG["max_show"]],
        "pool_history": sorted(
            ({"code": c, **v} for c, v in new_pool.items()),
            key=lambda x: str(x.get("last_date", "")), reverse=True,
        )[:20],
    }
    save_json(CFG["result_file"], res)
    log(f"4/5 完成：信号 {len(signals)} 门槛 {threshold} "
        f"达标={'是' if density_ok else '否'} 耗时 {time.time() - t0:.0f}s")
    log("5/5 已写 result.json")


if __name__ == "__main__":
    main()
