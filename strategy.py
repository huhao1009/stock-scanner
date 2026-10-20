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
  3. describe()       返回若干行文字，显示在报告底部，让人知道规则是什么
  4. evaluate()       核心：拿到一只股票的日线，判断它符不符合条件

evaluate() 的输入输出契约
-------------------------
输入：
    hist    pandas.DataFrame，单只股票的日线，最后一行是最新交易日
            字段：date / open / high / low / close / volume /
                  amount / outstanding_share / turnover
    code    带市场前缀的代码，如 "sz300466"
    name    股票名称
    prev    该股票上一次入池的记录 dict，第一次入池时为 None
    mktcap  流通市值（亿元），已经算好；算不出来时是 None

返回：
    None                                  不符合，直接丢弃
    {"level": None,   ...}                符合，进观察池，但今天不触发信号
    {"level": "强",   ...}                触发强信号（会推微信）
    {"level": "弱",   ...}                触发弱信号（只进报告）

返回的完整结构：
    {
      "level":   "强" / "弱" / None
      "score":   int 0~100，用于排序，越高越靠前
      "summary": str，一句话说明，显示在卡片标题下面那行灰字
      "fields":  [(标签, 值, 样式), ...]，卡片里的表格，样式可选
                 ""      普通加粗
                 "up"    红色（涨幅等）
                 "warn"  橙色（需要警惕）
                 "dim"   灰色（次要信息）
                 "full"  独占一整行（值跨三列）
      "why":     str，一段自然语言解释，显示在卡片底部灰底框里
      "metrics": dict，任意数值，会被原样写进 pool.json 供下次调用使用
    }

关键点：报告是"通用渲染"的——你往 fields 里放什么，报告就显示什么。
        想加一个新指标，只要在 fields 里加一行，不需要改 report.py。

=====================================================================
当前策略：横盘突破 v5
=====================================================================
思路：先判断一只股票"过去一段时间是不是在箱体里睡着"，再判断"今天有没有醒"。
      睡着 = 状态，醒 = 事件，两件事分开判断，所以池子独立于当天涨跌生成。
"""

import pandas as pd

NAME = "横盘突破"
VERSION = "v5"

# =====================================================================
# 参数区：所有阈值都在这里，改参数只改这一段
# =====================================================================
CFG = {
    # ---- 入池门槛：什么样的股票值得盯 ----
    "price_min": 3.0,        # 最低股价，剔除低价股
    "mkt_min": 15.0,         # 流通市值下限（亿元），剔除空壳
    "mkt_max": 50.0,         # 流通市值上限（亿元）

    # ---- 横盘形态：四条同时满足才算"睡着了" ----
    "box_days": 90,          # 箱体长度（交易日）
    "amp_max": 0.30,         # 箱体振幅上限（最高/最低-1）
    "ma_gap_max": 0.10,      # 5/10/20/60 日均线粘合度上限
    "turnover_max": 3.0,     # 横盘期日均换手率上限（%）

    # ---- 触发：今天有没有"醒" ----
    "pos_min": 50.0,         # 收盘价必须站上箱体这个百分位才允许触发
    "strong_vol": 2.0, "strong_chg": 3.0,   # 强信号
    "weak_vol": 1.6, "weak_chg": 2.0,       # 弱信号

    # ---- 提示 ----
    "chase_pos": 110.0,      # 箱位超过此值 → 已冲出货箱顶，标追高预警
}


def describe():
    """报告底部显示的规则说明。换策略时记得一起改。"""
    return [
        f"横盘判定：{CFG['box_days']}日振幅≤{CFG['amp_max']*100:.0f}%"
        f"／均线粘合≤{CFG['ma_gap_max']*100:.0f}%"
        f"／日均换手≤{CFG['turnover_max']:.0f}%",
        f"触发：强=量能≥{CFG['strong_vol']}倍且涨≥{CFG['strong_chg']:.0f}%"
        f" · 弱=量能≥{CFG['weak_vol']}倍且涨≥{CFG['weak_chg']:.0f}%",
        f"市值 {CFG['mkt_min']:.0f}~{CFG['mkt_max']:.0f} 亿 · 股价≥{CFG['price_min']:.0f} 元",
    ]


# =====================================================================
# 可交易范围：账户开不了的板块直接不扫，省数据省时间
# 开通对应权限后，把开关改成 False 即可恢复
# =====================================================================
UNIVERSE = {
    "exclude_star": True,     # 科创板 688/689：50万资金+2年经验
    "exclude_cyb": True,      # 创业板 300/301/302：10万资金+2年经验
    "exclude_bj": True,       # 北交所：50万资金
    "exclude_b": True,        # B股 900/200：美元/港币交易
}


def is_excluded(code):
    """按代码判断是否属于不扫的板块。代码形如 sh688001 / sz300466 / bj430047。"""
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
# 核心：判断一只股票
# =====================================================================
def evaluate(hist, code, name, prev, mktcap):
    # ---------- 0. 市值门槛 ----------
    if mktcap is None or mktcap < CFG["mkt_min"] or mktcap > CFG["mkt_max"]:
        return None

    # ---------- 1. 切出箱体和最新一天 ----------
    d = hist.tail(CFG["box_days"] + 1)
    if len(d) < CFG["box_days"] + 1:
        return None
    box, last = d.iloc[:-1], d.iloc[-1]

    lo, hi = float(box["low"].min()), float(box["high"].max())
    if lo <= 0 or hi <= lo:
        return None
    close = float(last["close"])

    # ---------- 2. 横盘判定 ----------
    amp = hi / lo - 1                                  # 箱体振幅
    if amp > CFG["amp_max"]:
        return None

    # 均线粘合只看箱体期（不含今天）：
    # 横盘是"过去的状态"，今天放量突破本身就该把短均线拉起来，
    # 如果把今天算进去，一只横得好好的票会因为自己今天涨了就判不合格。
    closes = box["close"]
    if len(closes) < 60:
        return None
    mas = [closes.tail(n).mean() for n in (5, 10, 20, 60)]
    ma_gap = (max(mas) - min(mas)) / min(mas)          # 均线粘合度
    if ma_gap > CFG["ma_gap_max"]:
        return None

    if "turnover" not in box.columns or box["turnover"].notna().sum() < 30:
        return None
    raw_t = float(box["turnover"].median())
    # 新浪接口的换手率是小数(0.0129=1.29%)，但不同版本可能直接给百分数，做个自适应
    scale = 100.0 if raw_t < 1.0 else 1.0
    t_avg = float(box["turnover"].mean()) * scale
    if t_avg > CFG["turnover_max"]:
        return None

    # ---------- 3. 今日异动 ----------
    vol_avg = float(box["volume"].mean())
    if vol_avg <= 0:
        return None
    vol_ratio = float(last["volume"]) / vol_avg         # 相对自身箱体期均量的倍数
    prev_close = float(box["close"].iloc[-1])
    chg = (close / prev_close - 1) * 100 if prev_close > 0 else 0.0
    pos = (close - lo) / (hi - lo) * 100                # 箱体位置，可超过100
    gap_to_top = (hi / close - 1) * 100                 # 距箱顶，已突破时为负
    broke = pos > 100.5                                 # 留 0.5% 容差，避免贴顶误判为突破

    # ---------- 4. 信号分档 ----------
    level = None
    if pos >= CFG["pos_min"]:
        if vol_ratio >= CFG["strong_vol"] and chg >= CFG["strong_chg"]:
            level = "强"
        elif vol_ratio >= CFG["weak_vol"] and chg >= CFG["weak_chg"]:
            level = "弱"

    # ---------- 5. 箱位文案（修掉"距箱顶-4.1%"这种反人类写法） ----------
    if broke:
        pos_txt = f"已突破箱顶 {pos - 100:.1f}%（箱位 {pos:.0f}%）"
        pos_kind = "warn" if pos >= CFG["chase_pos"] else "up"
    elif gap_to_top <= 3:
        pos_txt = f"贴箱顶（箱位 {pos:.0f}%）"
        pos_kind = "up"
    else:
        pos_txt = f"{pos:.0f}%（距箱顶 {gap_to_top:.1f}%）"
        pos_kind = ""

    # ---------- 6. 一句话标签 ----------
    if level == "强":
        if pos >= CFG["chase_pos"]:
            summary = f"强信号 · 但已冲出箱顶 {pos - 100:.0f}%，追高风险大"
        else:
            summary = f"强信号 · 量能 {vol_ratio:.1f} 倍 · 站上箱体 {pos:.0f}%"
    elif level == "弱":
        summary = f"弱信号 · 量能 {vol_ratio:.1f} 倍 · 接近触发"
    else:
        summary = f"横盘观察 · 箱位 {pos:.0f}% · 今日未放量"

    # ---------- 7. 卡片字段 ----------
    fields = [
        ("现价", f"¥{close:.2f}", ""),
        ("今日涨幅", f"{chg:+.2f}%", "up" if chg > 0 else ("" if chg == 0 else "dim")),
        ("量能倍数", f"{vol_ratio:.2f} 倍", "up" if vol_ratio >= CFG["strong_vol"] else ""),
        ("箱体位置", pos_txt, pos_kind),
        ("箱体振幅", f"{amp*100:.1f}%", ""),
        ("均线粘合", f"{ma_gap*100:.1f}%", ""),
        ("日均换手", f"{t_avg:.2f}%", ""),
        ("流通市值", f"{mktcap:.1f} 亿", ""),
        ("箱体区间", f"¥{lo:.2f} ~ ¥{hi:.2f}", "full"),
    ]

    # ---------- 8. 自然语言解释 ----------
    days_txt = f"本系统已跟踪 {prev['days'] + 1} 天，" if prev else "首次入池，"
    if broke:
        tail = (f"今日以 {vol_ratio:.1f} 倍量上涨 {chg:.1f}%，"
                f"收盘 {close:.2f} 已冲出箱顶 {hi:.2f}，属于启动之后的追高位置，"
                f"不是起步位置。")
    elif level:
        tail = (f"今日 {vol_ratio:.1f} 倍量上涨 {chg:.1f}%，"
                f"收盘 {close:.2f} 处于箱体 {pos:.0f}% 位置，箱顶 {hi:.2f}，"
                f"距箱顶还有 {gap_to_top:.1f}%。")
    else:
        tail = (f"今日量能 {vol_ratio:.1f} 倍、涨幅 {chg:.1f}%，还没有放量，"
                f"收盘 {close:.2f} 在箱体 {pos:.0f}% 位置，箱顶 {hi:.2f}。"
                f"属于「站在门口」，等的是它放量那天。")
    why = (f"{CFG['box_days']} 个交易日内股价被压在 ¥{lo:.2f}~¥{hi:.2f} 的箱子里，"
           f"振幅 {amp*100:.1f}%，四条均线粘合度 {ma_gap*100:.1f}%，"
           f"日均换手仅 {t_avg:.2f}%。{days_txt}{tail}")

    # ---------- 9. 打分 ----------
    score = _score(amp, ma_gap, vol_ratio, chg, pos, prev)
    if pos >= CFG["chase_pos"]:
        score -= 15                        # 追高扣分

    metrics = {
        "amp": round(amp * 100, 1),
        "ma_gap": round(ma_gap * 100, 1),
        "turnover_avg": round(t_avg, 2),
        "vol_ratio": round(vol_ratio, 2),
        "chg": round(chg, 2),
        "pos": round(pos, 1),
        "close": round(close, 2),
        "box_high": round(hi, 2),
        "box_low": round(lo, 2),
        "gap_to_top": round(gap_to_top, 1),
        "days": (prev["days"] + 1) if prev else 1,
    }

    return {
        "level": level,
        "score": max(0, min(int(score), 100)),
        "summary": summary,
        "fields": fields,
        "why": why,
        "metrics": metrics,
    }


def _score(amp, ma_gap, vol_ratio, chg, pos, prev):
    """打分：振幅越窄、均线越粘、跟踪越久 → 底分越高；量能和涨幅 → 加分。"""
    s = 40.0
    s += min((CFG["amp_max"] - amp) / CFG["amp_max"] * 15, 15)
    s += min((CFG["ma_gap_max"] - ma_gap) / CFG["ma_gap_max"] * 15, 15)
    days = (prev["days"] + 1) if prev else 1
    s += min(days, 30) * 0.4
    if vol_ratio > 1.2:
        s += min(vol_ratio, 6) * 1.8
    s += min(max(chg, 0), 10)
    return s
