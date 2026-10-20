# -*- coding: utf-8 -*-
"""
=====================================================================
报告渲染 + 微信推送
=====================================================================

这里是"通用渲染器"：strategy.py 给什么字段，报告就显示什么。
换策略不需要改这个文件 —— 前提是 strategy.evaluate() 按契约返回
level / score / summary / fields / why。

环境变量：
    SENDKEY     Server酱 SendKey
    REPORT_URL  报告网址（推送里附带链接）
=====================================================================
"""
import json
import os

SENDKEY = os.environ.get("SENDKEY", "").strip()
REPORT_URL = os.environ.get("REPORT_URL", "").strip()


def _rows(fields):
    """把 [(标签, 值, 样式), ...] 排成表格行；样式 full 的单独占一行。"""
    out, buf = [], []

    def flush():
        if not buf:
            return
        tds = "".join(f"<td>{lab}</td><td class='{k or 'v'}'>{val}</td>"
                      for lab, val, k in buf)
        if len(buf) == 1:
            tds += "<td></td><td></td>"
        out.append(f"<tr>{tds}</tr>")
        buf.clear()

    for item in fields:
        lab, val, kind = item
        if kind == "full":
            flush()
            out.append(f"<tr><td>{lab}</td><td class='v' colspan='3'>{val}</td></tr>")
        else:
            buf.append(item)
            if len(buf) == 2:
                flush()
    flush()
    return "".join(out)


def card(rec, idx):
    level = rec.get("level") or "观察"
    cls = {"强": "strong", "弱": "weak"}.get(level, "watch")
    badge = {"强": "强信号", "弱": "弱信号"}.get(level, "观察")
    return f"""<div class="card {cls}">
<div class="h"><span class="idx">{idx}</span><span class="nm">{rec['name']}</span>
<span class="cd">{rec['code']}</span><span class="sc">{rec['score']}分</span></div>
<div class="tag">{rec.get('summary') or badge}</div>
<table>{_rows(rec.get('fields') or [])}</table>
<div class="why">{rec.get('why', '')}</div>
</div>"""


def build_placeholder(date, reason):
    """非交易日 / 扫描被中止时也要出一张页面，否则工作流会因缺文件而变红。"""
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>横盘异动 {date}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
background:#f2f3f5;margin:0;padding:14px;color:#1a1a1a}}
h1{{font-size:19px;margin:0 0 4px}}
.sub{{color:#8a8a8a;font-size:12px;margin-bottom:18px}}
.box{{background:#fff;border-radius:12px;padding:22px 14px;text-align:center;
color:#888;font-size:13px;line-height:1.8}}
</style></head><body>
<h1>横盘异动选股</h1>
<div class="sub">{date}</div>
<div class="box">{reason}<br>今天没有数据，明天再来。</div>
</body></html>"""


def _watch_html(watch):
    if not watch:
        return '<p class="empty">池内暂无标的</p>'
    trs = []
    for w in watch:
        pos, gap = w.get("pos"), w.get("gap_to_top")
        if pos is None:
            pos_txt, status, cls = "-", "数据不足", ""
        elif pos >= 110:
            pos_txt, cls = f"{pos:.0f}%", "chase"
            status = f"已冲出箱顶 {pos - 100:.1f}%"
        elif pos >= 100:
            pos_txt, cls = f"{pos:.0f}%", "hot"
            status = f"刚突破箱顶 {pos - 100:.1f}%"
        elif pos >= 85:
            pos_txt, cls = f"{pos:.0f}%", "hot"
            status = f"🔥 贴箱顶（还差 {gap:.1f}%）" if gap is not None else "🔥 贴箱顶"
        elif pos >= 70:
            pos_txt, cls = f"{pos:.0f}%", "near"
            status = f"逼近箱顶（还差 {gap:.1f}%）" if gap is not None else "逼近箱顶"
        elif pos >= 45:
            pos_txt, cls = f"{pos:.0f}%", ""
            status = "箱体中部"
        else:
            pos_txt, cls = f"{pos:.0f}%", "cold"
            status = "箱体下部"
        trs.append(
            f"<tr class='{cls}'><td>{w['name']}<div class='cd'>{w['code']}</div></td>"
            f"<td class='c'>{w.get('days', 1)}天</td><td class='c'>{w.get('amp', '-')}%</td>"
            f"<td class='c pos'>{pos_txt}</td><td class='st'>{status}</td></tr>")
    return (
        '<table class="wl"><tr><th>名称</th><th>跟踪</th><th>振幅</th>'
        '<th>箱位</th><th>状态</th></tr>' + "".join(trs) + "</table>"
        '<div class="legend">箱位＝当前价在箱体中的位置，100% 即箱顶。<br>'
        '「跟踪」是本系统盯了它几天，不是它横了几天 —— 横盘周期固定按策略参数算。</div>')


def _watch_html(watch):
    """信号历史池：最近 14 天出过信号的票"""
    if not watch:
        return '<p class="empty">最近 14 天暂无信号记录</p>'
    trs = []
    for w in watch:
        sleep = w.get("sleep")
        tag = "⚠️" if (sleep is not None and sleep <= 3) else ""
        trs.append(
            f"<tr><td>{w['name']}<div class='cd'>{w['code']}</div></td>"
            f"<td class='c'>{w.get('last_date', '-')}</td>"
            f"<td class='c'>¥{w.get('close', '-')}</td>"
            f"<td class='c pos'>{tag}{sleep if sleep is not None else '-'}天</td>"
            f"<td class='st'>J={w.get('J', '-')}</td></tr>")
    return (
        '<table class="wl"><tr><th>名称</th><th>信号日</th><th>收盘</th>'
        '<th>休眠</th><th>J值</th></tr>' + "".join(trs) + "</table>"
        '<div class="legend">「休眠」= 距上一根单日涨跌≥4.7%的天数，'
        '越短说明越"刚动过"。休眠≤3天标注 ⚠️：连板概率较高，但波动大，'
        '整体收益并不更好，勿重仓。</div>')


def build(result):
    d = result
    strong, weak = d.get("strong", []), d.get("weak", [])
    hist_pool = d.get("pool_history", [])
    st = d.get("strategy") or {}

    n_total = d.get("signal_total", len(strong) + len(weak))
    thr = d.get("density_threshold")
    ok = d.get("density_ok")
    br = d.get("breadth")

    if n_total:
        state = "已达标 → 推送" if ok else f"未达标 {thr} → 仅记录，不出手"
        head = f"出票 {n_total} 只 · 密度{state}"
    else:
        head = "今日无信号（安静的日子，不出手是纪律）"

    if strong:
        body_s = "".join(card(m, i) for i, m in enumerate(strong, 1))
        sec_s = "今日信号（密度达标 · 已推送微信）"
    else:
        body_s = '<p class="empty">今日密度未达标，没有推送</p>'
        sec_s = "今日信号"
    body_w = "".join(card(m, i) for i, m in enumerate(weak, 1)) or \
        ("" if strong else '<p class="empty">今日无信号</p>')

    scope = "全市场" if d.get("scope") == "all" else "涨幅榜"
    stat = f"扫描 {d.get('scanned', 0)} 只"
    if d.get("scanned_ok"):
        stat += f"（有效 {d['scanned_ok']}）"
    br_txt = f" · 大盘涨家数 {br}%" if br is not None else ""

    rules = st.get("rules") or []
    rules_html = "".join(f"{r}<br>" for r in rules)

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>黑牛稳健 {d['date']}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f2f3f5;margin:0;padding:14px;color:#1a1a1a;-webkit-text-size-adjust:100%}}
h1{{font-size:19px;margin:0 0 4px}}
.sub{{color:#8a8a8a;font-size:12px;margin-bottom:14px;line-height:1.7}}
h2{{font-size:15px;margin:20px 0 10px;padding-left:8px;border-left:3px solid #c00}}
.card{{background:#fff;border-radius:12px;padding:13px;margin-bottom:11px;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.card.strong{{border-left:4px solid #c00}}
.card.weak{{border-left:4px solid #faad14}}
.h{{display:flex;align-items:baseline;gap:7px;margin-bottom:6px}}
.idx{{color:#bbb;font-size:13px}}
.nm{{font-size:17px;font-weight:700}}
.cd{{color:#aaa;font-size:11px}}
.sc{{margin-left:auto;background:#fff1f0;color:#c00;border-radius:9px;padding:2px 8px;font-size:12px;font-weight:700}}
.tag{{font-size:11px;color:#8a8a8a;margin-bottom:9px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:5px 2px;border-bottom:1px solid #f4f4f4}}
td:nth-child(odd){{color:#999;width:22%}}
.v{{color:#1a1a1a;font-weight:600}}
.up{{color:#c00;font-weight:700}}
.warn{{color:#e08a00;font-weight:700}}
.dim{{color:#999}}
.why{{margin-top:9px;font-size:12px;line-height:1.65;background:#fafbfc;border-radius:6px;padding:8px 9px;color:#555}}
.empty{{color:#b0b0b0;font-size:13px;text-align:center;padding:14px;background:#fff;border-radius:10px}}
.wl{{background:#fff;border-radius:10px;font-size:12px;overflow:hidden}}
.wl th{{background:#fafafa;color:#888;font-weight:500;padding:8px 6px;font-size:11px;text-align:left}}
.wl td{{padding:8px 6px;border-bottom:1px solid #f4f4f4}}
.wl .c{{text-align:center;color:#666}}
.wl .cd{{color:#bbb;font-size:10px;margin-top:2px}}
.wl .pos{{font-weight:700}}
.wl .st{{font-size:11px;color:#888}}
.legend{{font-size:11px;color:#aaa;margin-top:7px;line-height:1.7;padding:0 4px}}
.foot{{text-align:center;color:#bbb;font-size:11px;margin:22px 0 8px;line-height:1.8}}
</style></head><body>
<h1>黑牛稳健选股</h1>
<div class="sub">{d['date']} · {scope}{br_txt} · {stat}<br>{head}<br>
策略：{st.get('name', '')} {st.get('version', '')}</div>
<h2>{sec_s}</h2>
{body_s if strong else body_w}
{'<h2>同日其余信号（排序按休眠天数）</h2>' + body_w if strong and weak else ''}
<h2>信号历史 · 最近14天</h2>
{_watch_html(hist_pool)}
<div class="foot">{rules_html}
⚠️ 非投资建议，历史回测不代表未来收益，请自行决策</div>
</body></html>"""


def push(result):
    """仅密度达标日推送，节省 Server酱 每天 5 条额度。"""
    strong = result.get("strong", [])
    if not SENDKEY:
        print("未配置 SENDKEY，跳过推送")
        return
    if not strong:
        print("今日密度未达标/无信号，按规则不推送")
        return

    shown = strong[:12]
    names = "、".join(m["name"] for m in shown)
    more = f" 等{len(strong)}只" if len(strong) > len(shown) else ""
    title = f"黑牛信号 {result['date']}：{names[:24]}{more}"
    lines = [f"当日出票 **{result.get('signal_total', len(strong))} 只**"
             f"（门槛 {result.get('density_threshold')}，已达标）。"
             f"建议持有 20 个交易日口径，均匀买一篮子，不挑个股。", ""]
    for m in shown:
        f = {lab: val for lab, val, _ in (m.get("fields") or [])}
        lines.append(
            f"**{m['name']}** {m['code']}\n"
            f"- 现价 {f.get('现价', '-')} · 今日 {f.get('今日涨幅', '-')}"
            f" · J值 {f.get('J值', '-')}\n"
            f"- 休眠 {f.get('休眠天数', '-')} · 量能 {f.get('量能倍数', '-')}\n"
            f"- {m.get('summary', '')}\n")
    desp = "\n".join(lines)
    if REPORT_URL:
        desp += f"\n\n[📊 查看完整报告]({REPORT_URL})"
    desp += "\n\n⚠️ 仅供参考，非投资建议"
    try:
        import requests          # 只有推送时才需要，报告渲染不依赖网络库
        r = requests.post(f"https://sctapi.ftqq.com/{SENDKEY}.send",
                          data={"title": title[:32], "desp": desp}, timeout=20)
        j = r.json()
        print("推送成功" if j.get("code") == 0 else f"推送失败: {j}")
    except Exception as e:
        print("推送异常:", e)


if __name__ == "__main__":
    with open("result.json", encoding="utf-8") as f:
        res = json.load(f)

    if res.get("skip"):
        reason = res.get("reason") or "今天不是交易日"
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(build_placeholder(res.get("date", ""), reason))
        print(f"跳过：{reason}（已生成占位页，不推送）")
        raise SystemExit(0)

    html = build(res)
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("report.html 已生成")
    push(res)
