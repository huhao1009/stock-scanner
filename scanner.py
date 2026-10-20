
# -*- coding: utf-8 -*-
"""
A股横盘异动选股引擎 v4 (C方案: 强弱双档)
横盘池(状态) + 异动触发(事件)
强信号 -> 微信推送; 弱信号 -> 仅进报告
"""
import warnings
warnings.filterwarnings("ignore")
import time, re, json, datetime, os
import pandas as pd
import akshare as ak

CFG = {
    "price_min": 3.0,
    "mkt_min": 15, "mkt_max": 50,
    "box_days": 90,
    "amp_max": 0.30,
    "ma_gap_max": 0.10,
    "turnover_max": 3.0,
    # 强信号(推微信)
    "strong_vol": 2.0, "strong_chg": 3.0,
    # 弱信号(只进报告)
    "weak_vol": 1.6, "weak_chg": 2.0,
    "scan_chg_min": 2.0,      # 进入扫描的最低涨幅(控耗时)
    "max_scan_hot": 700,      # 涨幅榜最多扫描数(池内股票不受限)
    "sleep": 0.18,
    "max_out": 3,
    "max_watch": 15,
    "pool_file": "pool.json",
}
BAD = re.compile(r"ST|\*ST|退")


def is_trading_day(d=None):
    """判断是否为A股交易日; 日历获取失败时保守返回True(不阻断)"""
    d = d or datetime.date.today()
    try:
        cal = ak.tool_trade_date_hist_sina()
        days = set(pd.to_datetime(cal["trade_date"]).dt.date.tolist())
        return d in days
    except Exception as e:
        log(f"  交易日历获取失败({type(e).__name__}), 按交易日处理")
        return True


def log(m): print(m, flush=True)


def load_spot():
    for i in range(3):
        try:
            df = ak.stock_zh_a_spot()
            if df is not None and len(df) > 1000:
                df.columns = [str(c).strip() for c in df.columns]
                return df
        except Exception as e:
            log(f"  快照重试{i+1}: {e}")
            time.sleep(3)
    raise RuntimeError("快照获取失败")


def get_mktcap():
    """市值(亿)映射; 多源尝试, 失败不阻断"""
    # 源1: 东财实时(含总市值)
    try:
        df = ak.stock_zh_a_spot_em()
        df.columns = [str(c).strip() for c in df.columns]
        if "总市值" in df.columns:
            df["_c"] = df["代码"].astype(str).map(
                lambda x: ("sh" if x.startswith(("6", "9")) else "sz") + x)
            s = (df.set_index("_c")["总市值"] / 1e8).dropna()
            log(f"  市值来源: 东财, {len(s)} 只")
            return s.to_dict()
    except Exception as e:
        log(f"  东财市值失败({type(e).__name__})")
    # 源2: 新浪个股信息批量(备用, 较慢, 仅取部分)
    return {}


def fetch(code):
    for _ in range(2):
        try:
            h = ak.stock_zh_a_daily(symbol=code, adjust="qfq")
            if h is not None and len(h) > CFG["box_days"] + 35:
                return h
            return None
        except Exception:
            time.sleep(1)
    return None


def metrics(h):
    d = h.tail(CFG["box_days"] + 1).reset_index(drop=True)
    if len(d) < CFG["box_days"] + 1:
        return None
    box, last = d.iloc[:-1], d.iloc[-1]
    hi, lo = box["high"].max(), box["low"].min()
    if lo <= 0:
        return None
    amp = (hi - lo) / lo
    if amp > CFG["amp_max"]:
        return None
    closes = d["close"]
    if len(closes) < 60:
        return None
    mas = [closes.tail(n).mean() for n in (5, 10, 20, 60)]
    gap = (max(mas) - min(mas)) / min(mas)
    if gap > CFG["ma_gap_max"]:
        return None
    if "turnover" not in box.columns or box["turnover"].notna().sum() < 30:
        return None
    t_avg = box["turnover"].mean() * 100
    if t_avg > CFG["turnover_max"]:
        return None
    vol_avg = box["volume"].mean()
    if vol_avg <= 0:
        return None
    vr = last["volume"] / vol_avg
    chg = (last["close"] / box["close"].iloc[-1] - 1) * 100
    pos = (last["close"] - lo) / (hi - lo) * 100 if hi > lo else 0
    return {
        "amp": round(amp * 100, 1), "ma_gap": round(gap * 100, 1),
        "turnover_avg": round(t_avg, 2), "vol_ratio": round(vr, 2),
        "chg": round(chg, 2), "box_high": round(hi, 2), "box_low": round(lo, 2),
        "close": round(float(last["close"]), 2), "pos_in_box": round(pos, 1),
        "last_date": str(last["date"])[:10],
    }


def load_pool():
    if not os.path.exists(CFG["pool_file"]):
        return {}
    try:
        with open(CFG["pool_file"], encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def calc_mktcap(h):
    """用流通股本 x 收盘价 自算流通市值(亿), 不依赖东财"""
    try:
        last = h.iloc[-1]
        shares = float(last.get("outstanding_share") or 0)
        price = float(last["close"])
        if shares > 0 and price > 0:
            return shares * price / 1e8
    except Exception:
        pass
    return None


def score_of(m, days):
    s = 40
    s += min((CFG["amp_max"] - m["amp"] / 100) / CFG["amp_max"] * 15, 15)
    s += min((CFG["ma_gap_max"] - m["ma_gap"] / 100) / CFG["ma_gap_max"] * 15, 15)
    s += min(days, 30) * 0.4
    s += min(m["vol_ratio"], 6) * 1.8 if m["vol_ratio"] > 1.2 else 0
    s += min(max(m["chg"], 0), 10)
    return int(min(s, 100))


def main():
    t0 = time.time()
    today = datetime.date.today().isoformat()

    # 节假日/周末判断: 非交易日直接退出, 不扫描不推送
    if not is_trading_day():
        log(f"今天({today})不是A股交易日(周末或休市), 跳过")
        with open("result.json", "w", encoding="utf-8") as f:
            json.dump({"date": today, "skip": True, "reason": "非交易日",
                       "scanned": 0, "pool_size": 0, "strong": [], "weak": [],
                       "watchlist": [], "mkt_filter": True}, f, ensure_ascii=False, indent=2)
        return

    log("1/5 全市场快照")
    spot = load_spot()
    log(f"  {len(spot)} 只")

    log("2/5 过滤")
    df = spot.copy()
    df = df[~df["名称"].astype(str).str.contains(BAD)]
    df = df[~df["代码"].astype(str).str.startswith(("bj", "4", "8", "920"))]
    for c in ("最新价", "成交额", "涨跌幅"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["最新价", "涨跌幅"])
    df = df[df["最新价"] >= CFG["price_min"]]

    cap = get_mktcap()
    mkt_ok = bool(cap)
    if cap:
        df["mkt"] = df["代码"].map(cap)
        n0 = len(df)
        df_ = df[(df["mkt"].isna()) | ((df["mkt"] >= CFG["mkt_min"]) & (df["mkt"] <= CFG["mkt_max"]))]
        log(f"  市值{CFG['mkt_min']}-{CFG['mkt_max']}亿(东财): {n0} -> {len(df_)}")
        df = df_
    else:
        df["mkt"] = None
        log("  东财市值不可用, 改用自算流通市值(逐只)")

    hot = df[df["涨跌幅"] >= CFG["scan_chg_min"]].sort_values("涨跌幅", ascending=False)
    pool = load_pool()
    codes_in_df = set(df["代码"])
    pool_codes = [c for c in pool if c in codes_in_df]

    # 控耗时: 涨幅榜按"成交额加权"截断, 优先扫描放量的(更可能触发)
    # 池内股票全扫(它们才是触发的主要来源)
    if len(hot) > CFG["max_scan_hot"]:
        hot = hot.copy()
        hot["_s"] = hot["涨跌幅"] * (hot["成交额"].clip(upper=hot["成交额"].quantile(0.9)) ** 0.3)
        hot = hot.nlargest(CFG["max_scan_hot"], "_s")
        log(f"  涨幅榜按放量程度截取前{CFG['max_scan_hot']}只")

    scan_codes = list(dict.fromkeys(list(pool_codes) + list(hot["代码"])))
    log(f"3/5 扫描 {len(scan_codes)} 只 (异动{len(hot)} + 池内{len(pool_codes)})")

    info = df.set_index("代码")[["名称", "最新价", "涨跌幅", "mkt"]].to_dict("index")
    new_pool, strong, weak = {}, [], []
    for i, code in enumerate(scan_codes, 1):
        h = fetch(code)
        if i % 100 == 0:
            log(f"  {i}/{len(scan_codes)} 池{len(new_pool)} 强{len(strong)} 弱{len(weak)}")
        if h is None:
            continue
        m = metrics(h)
        time.sleep(CFG["sleep"])
        if m is None:
            continue
        # 自算市值并过滤(东财不可用时)
        if not mkt_ok:
            mc = calc_mktcap(h)
            if mc is None or mc < CFG["mkt_min"] or mc > CFG["mkt_max"]:
                continue
            info.setdefault(code, {})["mkt"] = mc
        elif (info.get(code, {}).get("mkt") or 0) == 0:
            mc = calc_mktcap(h)
            info.setdefault(code, {})["mkt"] = mc
        prev = pool.get(code, {})
        days = prev.get("days", 0) + 1 if prev else 1
        meta = info.get(code, {})
        rec = dict(m)
        rec.update({
            "code": code, "name": meta.get("名称", ""), "price": meta.get("最新价"),
            "chg_today": meta.get("涨跌幅"), "mkt": round(meta.get("mkt") or 0, 1),
            "first_seen": prev.get("first_seen", today), "days": days,
            "score": score_of(m, days),
        })
        new_pool[code] = {"first_seen": rec["first_seen"], "days": days,
                          "name": rec["name"], "amp": m["amp"], "last": today,
                          "pos": m["pos_in_box"], "close": m["close"],
                          "box_high": m["box_high"], "box_low": m["box_low"]}

        if m["pos_in_box"] < 50:
            continue
        if m["vol_ratio"] >= CFG["strong_vol"] and m["chg"] >= CFG["strong_chg"]:
            rec["level"] = "强"
            strong.append(rec)
        elif m["vol_ratio"] >= CFG["weak_vol"] and m["chg"] >= CFG["weak_chg"]:
            rec["level"] = "弱"
            weak.append(rec)

    with open(CFG["pool_file"], "w", encoding="utf-8") as f:
        json.dump(new_pool, f, ensure_ascii=False, indent=1)

    strong.sort(key=lambda x: -x["score"])
    weak.sort(key=lambda x: -x["score"])
    trig_codes = {r["code"] for r in strong + weak}
    # 排序: 先按箱体位置(越接近箱顶越靠前), 位置相近再看横盘天数
    watch = sorted([(k, v) for k, v in new_pool.items() if k not in trig_codes],
                   key=lambda x: (-(x[1].get("pos") or 0), -x[1]["days"]))

    res = {
        "date": today, "scanned": len(scan_codes), "pool_size": len(new_pool),
        "strong": strong[: CFG["max_out"]], "weak": weak[: CFG["max_out"]],
        "watchlist": [{"code": k, "name": v["name"], "days": v["days"],
                       "amp": v["amp"], "first_seen": v["first_seen"],
                       "pos": v.get("pos"), "close": v.get("close"),
                       "box_high": v.get("box_high"),
                       "gap_to_top": round((v["box_high"] / v["close"] - 1) * 100, 1)
                       if v.get("close") and v.get("box_high") else None}
                      for k, v in watch[: CFG["max_watch"]]],
        "mkt_filter": True,   # 自算市值已覆盖全部标的
    }
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    log(f"4/5 完成: 池{len(new_pool)} 强{len(strong)} 弱{len(weak)} 耗时{time.time()-t0:.0f}s")
    log("5/5 已写 result.json")


if __name__ == "__main__":
    main()
