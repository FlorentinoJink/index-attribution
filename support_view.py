"""Server-rendered evidence and walk-forward timing checks for the ETF screen."""

import math
from html import escape
from string import Template
from urllib.parse import urlencode

import sources
import support

MODES = {"candidate": "异常承接", "pressure": "全部承压日", "watch": "前一日预警"}


def ratio(n, total):
    return f"{int(n)} / {total}（{n / total:.1%}）" if total else "无样本"


def timing_table(frame):
    body = []
    recent = frame.loc["2024-01-01":]
    for label, sample in [
        ("2024 年起合计", recent),
        *recent.groupby(recent.index.str[:4]),
        ("2013–2023 年对照", frame.loc[:"2023-12-31"]),
    ]:
        settled = sample[sample.next_event.notna()]
        eligible = settled[settled.setup_pool]
        watch, others = eligible[eligible.watch], eligible[~eligible.watch]
        hits = int(watch.next_event.sum())
        # All new episodes with a fully observed previous five sessions, including
        # episodes whose preceding drawdown or volume failed the alert rule.
        total_starts = int(settled.loc[settled.quiet, "next_event"].sum())
        body.append(
            "<tr>"
            + "".join(
                f"<td>{s}</td>"
                for s in (
                    label,
                    ratio(hits, len(watch)),
                    ratio(others.next_event.sum(), len(others)),
                    ratio(hits, total_starts),
                    f"{watch.next_oc.mean():+.2f}%" if watch.next_oc.count() else "—",
                    f"{others.next_oc.mean():+.2f}%" if others.next_oc.count() else "—",
                )
            )
            + "</tr>"
        )
    return "".join(body)


def render(get):
    meta = support.snapshot(int(get("snapshot")) if get("snapshot") else None)
    run = meta["id"]
    day, feeds = support.study(run)
    live = support.forward_summary(run)
    years = sorted(set(day.index.str[:4]), reverse=True)
    year = get("year", years[0])
    mode = get("mode", "candidate")
    if mode not in MODES or year not in ["all", *years]:
        raise ValueError("年份或记录类型无效")
    selected = day if year == "all" else day[day.index.str[:4] == year]
    records = selected[selected[mode]].iloc[::-1]
    pages = max(1, math.ceil(len(records) / 20))
    page = min(pages, max(1, int(get("page", 1))))
    query = {"snapshot": run, "year": year, "mode": mode}

    def link(p, caption):
        params = escape(urlencode({**query, "page": p}))
        return f'<a href="/support?{params}">{caption}</a>'

    def opts(items, value):
        return "".join(
            f'<option value="{k}" {"selected" if k == value else ""}>{v}</option>'
            for k, v in items
        )

    rows = []
    for at, r in records.iloc[(page - 1) * 20 : page * 20].iterrows():
        state = (
            "放量回升"
            if r.candidate
            else (
                "放量未收复一半"
                if r.pressure and r.surge >= 2
                else ("普通量回升" if r.pressure and r.bounce else "普通量未收复一半")
                if r.pressure
                else "未触及承压线"
            )
        )
        result = (
            "待下一交易日"
            if r.watch and r.next_event != r.next_event
            else (
                "次日命中"
                if r.watch and r.next_event
                else "次日未命中"
                if r.watch
                else "—"
            )
        )
        parts = (
            at,
            f"{r.low_ret:+.2f}%",
            f"{r.close_ret:+.2f}%",
            *(f"{r[c]:.2f}×" for c in support.ETFS),
            state,
            ratio(r.control_bounces, int(r.controls)) if r.pressure else "—",
            result,
        )
        rows.append("<tr>" + "".join(f"<td>{x}</td>" for x in parts) + "</tr>")
    last = day.iloc[-1]
    settled = day.loc["2024-01-01":]
    settled = settled[settled.next_event.notna()]
    signals = settled[settled.watch]
    correct = int(signals.next_event.sum())
    pressure = selected[selected.pressure]
    heavy = pressure[pressure.surge >= 2]
    plain = pressure[pressure.surge < 2]
    sources_html = " · ".join(
        f'<a href="{escape(f["url"], quote=True)}">{f["code"]} 上交所原始日线</a>'
        for f in feeds
    )
    template = Template(
        (sources.ROOT / "web" / "support.html").read_text(encoding="utf8")
    )
    return template.substitute(
        run=run,
        first=day.index[0],
        last=day.index[-1],
        captured=escape(meta["collected"]),
        latest="触发" if last.watch else "未触发",
        quiet="满足" if last.quiet else "未满足",
        drawdown=f"{last.drawdown20:.2f}%",
        volume=int((last[list(support.ETFS)] >= 1.5).sum()),
        timing=timing_table(day),
        sources=sources_html,
        calls=len(signals),
        hits=correct,
        misses=len(signals) - correct,
        live_days=live["days"],
        live_calls=live["calls"],
        live_settled=live["settled"],
        live_hits=live["correct"],
        years=opts([("all", "全部历史"), *((y, y) for y in years)], year),
        modes=opts(MODES.items(), mode),
        count=len(pressure),
        candidates=int(pressure.candidate.sum()),
        heavy=ratio(heavy.bounce.sum(), len(heavy)),
        plain=ratio(plain.bounce.sum(), len(plain)),
        records="".join(rows) or '<tr><td colspan="9">所选区间没有这类记录</td></tr>',
        nav="　".join(
            filter(
                None,
                [
                    link(page - 1, "上一页") if page > 1 else "",
                    f"第 {page} / {pages} 页 · {len(records)} 条",
                    link(page + 1, "下一页") if page < pages else "",
                ],
            )
        ),
    )
