# -*- coding: utf-8 -*-
"""
=====================================================================
选股逻辑层 —— 整个系统里唯一"表达选股思想"的文件
=====================================================================

【替换指南】
以后要换策略，只改这个文件，scanner.py 和 report.py 一行都不用动。

一个策略只需要提供 4 样东西：

  1. NAME / VERSION   策略名字和版本号（显示在报告里）
  2. CFG              所有阈值参数，集中放这里，调参数不用翻代码
  3. describe()       返回若干行文字，显示在报告底部
  4. evaluate()       核心：拿到一只股票的日线，判断它符不符合条件
     set_market()     盘前把大盘宽度告诉策略（scanner 每天调用一次）

evaluate() 的输入输出契约
-------------------------
输入：
    hist    pandas.DataFrame，单只股票的日线，最后一行是最新交易日
            字段：date / open / high / low / close / volume /
                  amount / outstanding_share / turnover
    code    带市场前缀的代码，如 "sz000001"
    name    股票名称
    prev    该股票上一次的记录 dict，第一次为 None
    mktcap  流通市值（亿元），已经算好；算不出来时是 None

返回：
    None                今天不出信号
    {"level": "弱", ...}  出信号（进报告；只有当日信号总数达到密度门槛
                          时，scanner 才把它升级为 "强" 并推送微信）

返回结构：level / score / summary / fields / why / metrics
    fields 是 (标签, 值, 样式) 列表，样式可选 ""/"up"/"warn"/"dim"/"full"，
    报告通用渲染 —— 想加指标只要在 fields 里加一行。

=====================================================================
当前策略：黑牛稳健 v1（2026-10-20，1000 只主板样本回测通过）
=====================================================================
信号：EMA(C,5) 上穿 EMA(SLOPE(C,21)*20+C, 55)
过滤：改良KDJ J<50（不追高）
     + 收盘 < HHV(H,20)×0.94（买回调）
     + 收阳（C>O）
     + 当日大盘涨家数 ≥ 50%

回测结论（bt_top3/，2022-08~2026-10）：
    次日胜率 54.6% vs 基准 47.7%；价值集中在高密度日：
    当日出票 7-19 只 → 20日 +4.56%；20只以上 → 次日 75.9%、20日 +8.17%
    稀疏日（1-6只）≈ 无超额 → 密度门槛必须保留（scanner 实现）
    连板特征：休眠短的信号连板率高 4~6 倍，但整体收益并不更好 ——
    报告只标注，不宣称"精选"。
    建议持有口径：20 个交易日。
"""

import numpy as np
import pandas as pd

NAME = "黑牛稳健"
VERSION = "v1"

# =====================================================================
# 参数区
# =====================================================================
CFG = {
    "price_min": 3.0,        # 最低股价
    "mkt_min": 15.0,         # 流通市值下限（亿）—— 与回测口径一致
    "mkt_max": 300.0,        # 流通市值上限（亿）

    "slope_n": 21,           # 趋势线斜率周期
    "sell_n": 55,            # 卖线 EMA 周期
    "buy_n": 5,              # 买线 EMA 周期

    "j_max": 50.0,           # 改良KDJ J 上限（不追高）
    "hhv_n": 20,             # 回调参照的高点周期
    "pullback": 0.94,        # 收盘须低于 HHV×此值（买回调）

    "breadth_min": 0.50,     # 大盘涨家数占比下限
    "hold_days": 20,         # 建议持有交易日（回测口径）

    "sleep_big_move": 4.7,   # 单日涨跌幅达到此%记为"动过"
    "sleep_lookback": 120,   # 休眠天数最多往回看多少天
    "lb_tag_sleep": 3,       # 休眠≤此天数 → 标"博连板候选"
    "min_bars": 130,         # 指标预热需要的最少K线数
}

# 大盘宽度：scanner 盘后算好通过 set_market() 注入，evaluate 只读
_MARKET = {"breadth": None}


def set_market(breadth):
    """breadth：当日上涨家数占比（0~1）。None 表示未知，按不达标处理。"""
    _MARKET["breadth"] = breadth


def describe():
    """报告底部显示的规则说明。换策略时记得一起改。"""
    return [
        f"信号：EMA(C,{CFG['buy_n']}) 上穿 EMA(SLOPE(C,{CFG['slope_n']})"
        f"*20+C,{CFG['sell_n']})",
        f"过滤：J<{CFG['j_max']:.0f} ＋ 收盘<HHV(H,{CFG['hhv_n']})"
        f"×{CFG['pullback']:.2f} ＋ 收阳 ＋ 大盘涨家数≥{CFG['breadth_min']*100:.0f}%",
        f"密度门槛：当日出票≥7只（约0.7%×扫描数）才推送；"
        f"稀疏日仅记录不推送",
        f"建议持有 {CFG['hold_days']} 个交易日；休眠≤{CFG['lb_tag_sleep']}天"
        f"的信号标注「博连板候选」（连板率高4~6倍，但整体收益不更好，勿重仓）",
        f"市值 {CFG['mkt_min']:.0f}~{CFG['mkt_max']:.0f} 亿 · "
        f"股价≥{CFG['price_min']:.0f} 元",
    ]


# =====================================================================
# 可交易范围：账户开不了的板块直接不扫
# 开通对应权限后，把开关改成 False 即可恢复
# =====================================================================
UNIVERSE = {
    "exclude_star": True,     # 科创板 688/689：50万资金+2年经验
    "exclude_cyb": True,      # 创业板 300/301/302：10万资金+2年经验
    "exclude_bj": True,       # 北交所：50万资金
    "exclude_b": True,        # B股 900/200：美元/港币交易
}


def is_excluded(code):
    """按代码判断是否属于不扫的板块。代码形如 sh600000 / sz000001 / bj430047。"""
    raw = str(code).lower()
    if raw.startswith("bj"):
        return UNIVERSE["exclude_bj"]                 # 北交所
    num = raw[2:] if raw[:2] in ("sh", "sz") else raw
    if UNIVERSE["exclude_star"] and num.startswith(("688", "689")):
        return True                                   # 科创板
    if UNIVERSE["exclude_cyb"] and num.startswith(("300", "301", "302")):
        return True                                   # 创业板
    if UNIVERSE["exclude_b"] and num.startswith(("900", "200")):
        return True                                   # B股
    return False


# =====================================================================
# 指标（与通达信逐行对齐，同 bt_top3/backtest.py）
# =====================================================================
def EMA(s, n):
    return s.ewm(span=n, adjust=False).mean()


def SMA(s, n, m):
    """通达信 SMA(X,N,M): Y = (M*X + (N-M)*Y')/N"""
    return s.ewm(alpha=m / n, adjust=False).mean()


def SLOPE(s, n):
    """rolling 线性回归斜率（向量化）"""
    a = s.to_numpy(dtype=float)
    out = np.full(len(a), np.nan)
    if len(a) < n:
        return pd.Series(out, index=s.index)
    t = np.arange(n, dtype=float)
    t_c = t - t.mean()
    var = (t_c ** 2).sum()
    win = np.lib.stride_tricks.sliding_window_view(a, n)
    ym = win.mean(axis=1, keepdims=True)
    cov = ((win - ym) * t_c).sum(axis=1)
    out[n - 1:] = cov / var
    return pd.Series(out, index=s.index)


def modified_j(close, low, high):
    """改良KDJ：J = 3*SMA(RSV,5,1) - 2*SMA(SMA(RSV,5,1),3,1)，RSV 周期 9"""
    hh, ll = high.rolling(9).max(), low.rolling(9).min()
    rsv = ((close - ll) / (hh - ll) * 100).clip(0, 200)
    k = SMA(rsv, 5, 1)
    d = SMA(k, 3, 1)
    return 3 * k - 2 * d


def sleep_days(close, big_move, lookback):
    """休眠天数：距上一根单日涨跌幅≥big_move% 的天数（含当日），上限 lookback"""
    pct = close.pct_change() * 100
    big = (pct.abs() >= big_move).to_numpy()
    last = -1
    out = np.full(len(big), float(lookback))
    for i in range(len(big)):
        if big[i]:
            last = i
        out[i] = i - last if last >= 0 else lookback
    return float(out[-1])


# =====================================================================
# 核心：判断一只股票
# =====================================================================
def evaluate(hist, code, name, prev, mktcap):
    # ---------- 0. 门槛：市值 / 数据长度 / 大盘宽度 ----------
    if mktcap is None or mktcap < CFG["mkt_min"] or mktcap > CFG["mkt_max"]:
        return None
    if len(hist) < CFG["min_bars"]:
        return None
    breadth = _MARKET["breadth"]
    if breadth is None or breadth < CFG["breadth_min"]:
        return None

    c = hist["close"].astype(float)
    o = hist["open"].astype(float)
    h = hist["high"].astype(float)
    l = hist["low"].astype(float)
    v = hist["volume"].astype(float)

    # ---------- 1. 指标 ----------
    buy = EMA(c, CFG["buy_n"])
    sell = EMA(SLOPE(c, CFG["slope_n"]) * 20 + c, CFG["sell_n"])
    cross = bool(buy.iloc[-1] > sell.iloc[-1] and
                 buy.iloc[-2] <= sell.iloc[-2])
    if not cross:
        return None

    j = float(modified_j(c, l, h).iloc[-1])
    hhv = float(h.tail(CFG["hhv_n"]).max())
    close = float(c.iloc[-1])
    cheap = close < hhv * CFG["pullback"]
    yang = close > float(o.iloc[-1])
    if not (j < CFG["j_max"] and cheap and yang):
        return None

    # ---------- 2. 特征值（排序/标注用，全部只含当日及以前） ----------
    chg = (close / float(c.iloc[-2]) - 1) * 100
    vr = float(v.iloc[-1] / v.rolling(20).mean().iloc[-1])
    dd = (1 - close / (hhv * CFG["pullback"])) * 100      # 回调深度 %
    spread = (float(buy.iloc[-1]) / float(sell.iloc[-1]) - 1) * 100
    sleep = sleep_days(c, CFG["sleep_big_move"], CFG["sleep_lookback"])
    lb_tag = sleep <= CFG["lb_tag_sleep"]

    # ---------- 3. 文案 ----------
    if lb_tag:
        summary = (f"信号 · 刚动过（休眠{sleep:.0f}天）· 博连板候选："
                   f"连板概率高但波动大，勿重仓")
    else:
        summary = f"信号 · 休眠{sleep:.0f}天 · 建议持有{CFG['hold_days']}日口径"

    fields = [
        ("现价", f"¥{close:.2f}", ""),
        ("今日涨幅", f"{chg:+.2f}%", "up" if chg > 0 else ""),
        ("量能倍数", f"{vr:.2f} 倍", "up" if vr >= 1.2 else ""),
        ("J值", f"{j:.1f}", "dim"),
        ("回调深度", f"{dd:.1f}%", "dim"),
        ("休眠天数", f"{sleep:.0f} 天", "warn" if lb_tag else ""),
        ("金叉乖离", f"{spread:+.2f}%", "dim"),
        ("流通市值", f"{mktcap:.1f} 亿", ""),
        ("持有建议", f"{CFG['hold_days']} 个交易日口径"
         f"（回测20日均收 +1.8%）", "full"),
    ]

    why = (f"买线 EMA{CFG['buy_n']} 今日上穿趋势线 EMA{CFG['sell_n']}（金叉乖离 "
           f"{spread:+.2f}%）。J={j:.1f} 未过热；收盘 ¥{close:.2f} 低于 20 日高点 "
           f"¥{hhv:.2f} 的 {CFG['pullback']:.2f} 倍（回调深度 {dd:.1f}%）；"
           f"今日收阳。当日大盘涨家数 {breadth*100:.0f}%，"
           f"满足 ≥{CFG['breadth_min']*100:.0f}% 的市场环境条件。"
           f"该股已休眠 {sleep:.0f} 天（单日涨跌≥{CFG['sleep_big_move']}%记为动过）。"
           f"回测口径：持有 {CFG['hold_days']} 个交易日。")

    # score 用于报告排序：休眠越短越靠前（连板特征），仅排序不宣称优势
    score = int(round(100 - min(sleep, 60) / 60 * 40))

    metrics = {
        "close": round(close, 2),
        "chg": round(chg, 2),
        "vr": round(vr, 2),
        "J": round(j, 1),
        "dd": round(dd, 2),
        "spread": round(spread, 2),
        "sleep": int(sleep),
        "lb_tag": int(lb_tag),
        "breadth": round(breadth * 100, 1),
    }

    return {
        "level": "弱",            # 由 scanner 按密度门槛决定是否升级为"强"
        "score": max(0, min(score, 100)),
        "summary": summary,
        "fields": fields,
        "why": why,
        "metrics": metrics,
    }
