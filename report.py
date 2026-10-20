
# -*- coding: utf-8 -*-
"""生成手机版HTML报告 + 微信推送(仅强信号)"""
import json, os, requests

SENDKEY = os.environ.get("SENDKEY", "").strip()
REPORT_URL = os.environ.get("REPORT_URL", "").strip()


def card(m, idx):
    lvl = "强" if m.get("level") == "强" else "弱"
    cls = "strong" if lvl == "强" else "weak"
    return f"""<div class="card {cls}">
<div class="h"><span class="idx">{idx}</span><span class="nm">{m['name']}</span>
<span class="cd">{m['code']}</span><span class="sc">{m['score']}分</span></div>
<div class="tag">{lvl}信号 · 横盘池第{m['days']}天 · 市值{m['mkt']}亿</div>
<table>
<tr><td>现价</td><td class="v">¥{m['close']}</td><td>今日涨幅</td><td class="up">+{m['chg']}%</td></tr>
<tr><td>量比</td><td class="v">{m['vol_ratio']}倍</td><td>箱体位置</td><td class="v">{m['pos_in_box']}%</td></tr>
<tr><td>横盘振幅</td><td class="v">{m['amp']}%</td><td>均线粘合</td><td class="v">{m['ma_gap']}%</td></tr>
<tr><td>箱体区间</td><td class="v" colspan="3">¥{m['box_low']} ~ ¥{m['box_high']}</td></tr>
</table>
<div class="why">横盘{m['days']}个交易日，箱体振幅{m['amp']}%（均线粘合{m['ma_gap']}%，日均换手{m['turnover_avg']}%），
今日{m['vol_ratio']}倍量上涨{m['chg']}%，收盘{m['close']}处于箱体{m['pos_in_box']}%位置，箱顶{m['box_high']}。</div>
</div>"""


def build(result):
    d = result
    strong, weak = d.get("strong", []), d.get("weak", [])
    watch = d.get("watchlist", [])

    if strong or weak:
        head = f"强信号 {len(strong)} 只 · 弱信号 {len(weak)} 只"
    else:
        head = "今日无触发信号"

    body_s = "".join(card(m, i) for i, m in enumerate(strong, 1)) or '<p class="empty">今日无强信号</p>'
    body_w = "".join(card(m, i) for i, m in enumerate(weak, 1)) or '<p class="empty">今日无弱信号</p>'

    if watch:
        rows = "".join(
            f"<tr><td>{w['name']}</td><td>{w['code']}</td><td class='c'>{w['days']}天</td>"
            f"<td class='c'>{w['amp']}%</td><td class='c'>{w['first_seen']}</td></tr>"
            for w in watch)
        watch_html = f"""<table class="wl"><tr><th>名称</th><th>代码</th><th>池内天数</th><th>振幅</th><th>入池日</th></tr>{rows}</table>"""
    else:
        watch_html = '<p class="empty">池内暂无标的</p>'

    mkt_note = "" if d.get("mkt_filter") else '<div class="warn">⚠️ 本轮市值数据源不可用，未按市值过滤</div>'

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>横盘异动 {d['date']}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f2f3f5;margin:0;padding:14px;color:#1a1a1a;-webkit-text-size-adjust:100%}}
h1{{font-size:19px;margin:0 0 4px}}
.sub{{color:#8a8a8a;font-size:12px;margin-bottom:14px}}
.warn{{background:#fff7e6;border-left:3px solid #faad14;padding:8px 10px;border-radius:6px;font-size:12px;color:#96640a;margin-bottom:12px}}
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
.why{{margin-top:9px;font-size:12px;line-height:1.6;background:#fafbfc;border-radius:6px;padding:8px 9px;color:#555}}
.empty{{color:#b0b0b0;font-size:13px;text-align:center;padding:14px;background:#fff;border-radius:10px}}
.wl{{background:#fff;border-radius:10px;font-size:12px;overflow:hidden}}
.wl th{{background:#fafafa;color:#888;font-weight:500;padding:8px 6px;font-size:11px;text-align:left}}
.wl td{{padding:8px 6px;border-bottom:1px solid #f4f4f4}}
.wl .c{{text-align:center;color:#666}}
.foot{{text-align:center;color:#bbb;font-size:11px;margin:22px 0 8px;line-height:1.7}}
</style></head><body>
<h1>横盘异动选股</h1>
<div class="sub">{d['date']} · 扫描{d['scanned']}只 · 横盘池{d['pool_size']}只 · {head}</div>
{mkt_note}
<h2>强信号（已推送）</h2>
{body_s}
<h2>弱信号（接近触发）</h2>
{body_w}
<h2>横盘池 · 持续观察</h2>
{watch_html}
<div class="foot">阈值：横盘90日/振幅≤30%/均线粘合≤10%/日均换手≤3%<br>
强信号=量比≥2且涨≥3% · 弱信号=量比≥1.6且涨≥2%<br>
⚠️ 非投资建议，横盘突破策略胜率有限，请自行决策</div>
</body></html>"""


def push(result):
    """仅强信号推送; 无强信号不推, 节省 Server酱 5条/天额度"""
    strong = result.get("strong", [])
    if not SENDKEY:
        print("未配置 SENDKEY，跳过推送"); return
    if not strong:
        print("今日无强信号，按规则不推送"); return

    names = "、".join(m["name"] for m in strong)
    title = f"横盘异动 {result['date']}：{names}"
    lines = []
    for m in strong:
        lines.append(
            f"**{m['name']}** {m['code']} · {m['score']}分\n"
            f"- 现价 ¥{m['close']}，今日 **+{m['chg']}%**\n"
            f"- {m['vol_ratio']}倍量 · 箱体{m['pos_in_box']}%位 · 横盘{m['days']}天\n"
            f"- 振幅{m['amp']}%，箱顶¥{m['box_high']}\n")
    desp = "\n".join(lines)
    if REPORT_URL:
        desp += f"\n\n[📊 查看完整报告]({REPORT_URL})"
    desp += "\n\n⚠️ 仅供参考，非投资建议"
    try:
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
        print(f"今日非交易日（{res.get('date')}），跳过报告与推送")
        raise SystemExit(0)

    html = build(res)
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("report.html 已生成")
    push(res)
