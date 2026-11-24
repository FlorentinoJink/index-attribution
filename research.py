"""Daily arithmetic-return attribution, walk-forward calibration and linked contributions."""

from functools import lru_cache

import numpy as np
import pandas as pd

from sources import SPECS, TARGETS, annotations, read_series

MODELS = {
    "combined": ["market", "bond", "dxy", "yen"],
    "domestic": ["market", "bond"],
    "macro": ["bond", "dxy", "yen"],
}
WINDOWS = (126, 252, 504)
EVENTS = [
    (
        "2008-11-09",
        "扩大内需十项措施公布",
        "财政政策",
        True,
        "https://fukuoka.china-consulate.gov.cn/ggxw/200811/t20081110_5570229.htm",
    ),
    (
        "2014-11-17",
        "沪港通开通",
        "市场制度",
        True,
        "https://www.shanghai.gov.cn/nw5827/20200905/0001-5827_951473.html",
    ),
    (
        "2015-08-11",
        "人民币中间价报价机制调整",
        "汇率政策",
        True,
        "https://iefi.mof.gov.cn/pdlb/wmkzg/201508/t20150811_1414468.html",
    ),
    (
        "2021-03-11",
        "十四五规划纲要获批准",
        "五年规划",
        False,
        "https://app.www.gov.cn/govdata/gov/202103/12/469291/article.html",
    ),
    (
        "2023-02-17",
        "全面注册制制度规则发布",
        "市场制度",
        False,
        "https://www.csrc.gov.cn/csrc/c100028/c7123213/content.shtml",
    ),
    (
        "2024-09-24",
        "金融支持经济高质量发展发布会",
        "货币政策",
        True,
        "https://fushun.gov.cn/006/006022/006022005/20240925/8584e106-c341-4c79-b1c7-3241389d2c46.html",
    ),
    (
        "2026-03-12",
        "十五五规划纲要获批准",
        "五年规划",
        False,
        "https://www.ln.gov.cn/web/qmzx/2026qglh/hydt/2026031310030648625/index.shtml",
    ),
]
PLANS = [
    ("十五五", 2026, 2030),
    ("十四五", 2021, 2025),
    ("十三五", 2016, 2020),
    ("十二五", 2011, 2015),
    ("十一五", 2006, 2010),
    ("十五", 2001, 2005),
]
EXTRA = ["alpha", "residual", "unmodeled"]
LABELS = {
    **{k: v[1] for k, v in SPECS.items()},
    "alpha": "截距项",
    "residual": "模型残差",
    "unmodeled": "未建模交易日",
}


@lru_cache(maxsize=48)
def levels(run, key):
    values = pd.Series(dict(read_series(run, key)), dtype=float)
    values.index = pd.to_datetime(values.index)
    return values.sort_index()


def lagged_foreign(source, dates):
    # Last dated observation strictly before the China session; cover foreign holidays,
    # but never project beyond the first domestic session after the series ends.
    idx = source.index.searchsorted(dates, side="left") - 1
    safe = idx.clip(0)
    origin = source.index[safe]
    age = (dates - origin).days
    limit = dates.searchsorted(source.index[-1], side="right")
    valid = (idx >= 0) & (age <= 5) & (np.arange(len(dates)) <= limit)
    return pd.Series(np.where(valid, source.to_numpy()[safe], np.nan), index=dates)


@lru_cache(maxsize=20)
def daily(run, target, model, window, timing, backfill):
    if (
        target not in TARGETS
        or model not in MODELS
        or window not in WINDOWS
        or timing not in ("close", "lag")
    ):
        raise ValueError("指数或模型参数无效")
    factors = MODELS[model]
    benchmark = levels(run, "market")
    price = levels(run, target)
    # Use the benchmark's actual sessions; some provider series contain extra holiday rows.
    price = price.loc[price.index.intersection(benchmark.index)]
    dates = price.index
    out = pd.DataFrame(
        {
            "level": price,
            "benchmark": benchmark.reindex(dates),
            "ret": price.pct_change(fill_method=None) * 100,
        }
    )
    for key in factors:
        series = levels(run, key)
        aligned = (
            lagged_foreign(series, dates)
            if key in ("dxy", "yen")
            else series.reindex(dates)
        )
        r = aligned.pct_change(fill_method=None) * 100
        out["x_" + key] = r.shift(1) if timing == "lag" else r
    x = out[["x_" + k for k in factors]].to_numpy()
    y = out.ret.to_numpy()
    n = len(y)
    coefs = np.full((n, len(factors) + 1), np.nan)
    mean = np.full(n, np.nan)
    valid = np.isfinite(y) & np.isfinite(x).all(axis=1)
    if not backfill:
        release = max(
            SPECS[target][3],
            SPECS["market"][3] if "market" in factors else "1900-01-01",
        )
        valid &= dates >= pd.Timestamp(release)
    for i in range(window, n):
        if not valid[i]:
            continue
        mask = valid[i - window : i]
        if mask.sum() < int(np.ceil(window * 0.95)):
            continue
        xx = x[i - window : i][mask]
        yy = y[i - window : i][mask]
        mu = xx.mean(axis=0)
        sd = xx.std(axis=0)
        if np.any(sd < 1e-10):
            continue
        design = np.column_stack([np.ones(len(yy)), (xx - mu) / sd])
        b, _, rank, singular = np.linalg.lstsq(design, yy, rcond=None)
        if rank != design.shape[1] or singular[0] / singular[-1] > 1e6:
            continue
        beta = b[1:] / sd
        coefs[i] = np.r_[b[0] - mu @ beta, beta]
        mean[i] = yy.mean()
    out["baseline"] = mean
    out["alpha"] = coefs[:, 0]
    for j, k in enumerate(factors):
        out["beta_" + k] = coefs[:, 1 + j]
        out[k] = x[:, j] * coefs[:, 1 + j]
    out["fit"] = out[[*factors, "alpha"]].sum(axis=1, skipna=False)
    out["residual"] = out.ret - out.fit
    out["unmodeled"] = out.ret.where(out.fit.isna(), 0)
    out.index.name = "date"
    return out


def window_events(history, start, end):
    prices = history.level
    rows = []
    for date, title, group, same_day, source in sorted(EVENTS + annotations()):
        ts = pd.Timestamp(date)
        if not start <= ts <= end or ts < prices.index[0] or ts > prices.index[-1]:
            continue
        i = prices.index.searchsorted(ts, side="left" if same_day else "right")
        if not 0 < i < len(prices):
            continue
        metrics = [
            float((prices.iloc[i + h - 1] / prices.iloc[i - 1] - 1) * 100)
            if i + h - 1 < len(prices) and prices.index[i + h - 1] <= end
            else None
            for h in (1, 5, 20)
        ]
        days = history.iloc[i : i + 5]
        residual5 = None
        if len(days) == 5 and days.index[-1] <= end and days.residual.notna().all():
            weights = (1 + days.ret / 100).cumprod().shift(1, fill_value=1)
            residual5 = float((weights * days.residual).sum())
        rows.append(
            {
                "date": date,
                "title": title,
                "group": group,
                "anchor": str(prices.index[i].date()),
                "moves": metrics,
                "residual5": residual5,
                "source": source,
            }
        )
    return rows


def compare_models(run, target, window, timing, backfill, d):
    # Identical out-of-sample dates and one common past-only reference forecast.
    frames = {
        k: daily(run, target, k, window, timing, backfill).reindex(d.index)
        for k in MODELS
    }
    predictions = pd.DataFrame({k: f.fit for k, f in frames.items()}).dropna()
    y = d.ret.reindex(predictions.index)
    ref = frames["combined"].baseline.reindex(predictions.index)
    denom = ((y - ref) ** 2).sum()
    metrics = []
    for k in MODELS:
        errors = y - predictions[k]
        metrics.append(
            {
                "model": k,
                "rmse": float(np.sqrt((errors**2).mean())) if len(y) else None,
                "r2": float(1 - (errors**2).sum() / denom)
                if len(y) >= 20 and denom > 1e-10
                else None,
            }
        )
    return {"n": len(y), "metrics": metrics}


def regimes(history):
    result = []
    for name, begin, end in PLANS:
        d = history.loc[f"{begin}-01-01" : f"{end}-12-31"].dropna(subset=["ret"])
        if len(d) < 2:
            continue
        path = (1 + d.ret / 100).cumprod().to_numpy()
        peak = np.maximum.accumulate(np.r_[1.0, path])[1:]
        result.append(
            {
                "name": name,
                "year": begin,
                "first": str(d.index[0].date()),
                "last": str(d.index[-1].date()),
                "count": len(d),
                "total": (path[-1] - 1) * 100,
                "drawdown": float((path / peak - 1).min() * 100),
                "volatility": float(d.ret.std() * np.sqrt(252)),
            }
        )
    return result


def select(run, target, model, window, timing, backfill, start, end):
    all_days = daily(run, target, model, window, timing, backfill)
    begin = pd.Timestamp(start) if start else all_days.index[0]
    finish = pd.Timestamp(end) if end else all_days.index[-1]
    if pd.isna(begin) or pd.isna(finish) or begin > finish:
        raise ValueError("日期区间无效")
    part = all_days.loc[begin:finish].dropna(subset=["ret"])
    if part.empty:
        raise ValueError("该指数在所选区间没有收益观测")
    return all_days, part


def analyze(run, target, model, window, timing, backfill, start, end):
    history, d = select(run, target, model, window, timing, backfill, start, end)
    factors = MODELS[model]
    components = [*factors, *EXTRA]
    wealth = (1 + d.ret / 100).cumprod()
    before = wealth.shift(1, fill_value=1)
    linked = d[components].fillna(0).mul(before, axis=0).sum()
    cumulative = (wealth.iloc[-1] - 1) * 100
    if not np.isclose(linked.sum(), cumulative, atol=1e-7):
        raise ArithmeticError("收益贡献未对账")
    fitted = d.dropna(subset=["fit"])
    ref = ((fitted.ret - fitted.baseline) ** 2).sum()
    r2 = (
        1 - (fitted.residual**2).sum() / ref
        if ref > 1e-10 and len(fitted) >= 20
        else None
    )
    cols = [
        "level",
        "benchmark",
        "ret",
        "fit",
        *components,
        *["x_" + k for k in factors],
        *["beta_" + k for k in factors],
    ]
    base_pos = history.index.get_loc(d.index[0]) - 1
    base = history.iloc[base_pos]
    return {
        "target": target,
        "run": run,
        "model": model,
        "window": window,
        "timing": timing,
        "backfill": backfill,
        "first": str(d.index[0].date()),
        "last": str(d.index[-1].date()),
        "base_date": str(base.name.date()),
        "base_level": base.level,
        "base_benchmark": base.benchmark,
        "total": cumulative,
        "count": len(d),
        "modeled": len(fitted),
        "r2": r2,
        "fitted_last": str(fitted.index[-1].date()) if len(fitted) else None,
        "factors": factors,
        "components": components,
        "contrib": linked.to_dict(),
        "cols": cols,
        "dates": d.index.strftime("%Y-%m-%d").tolist(),
        "rows": d[cols]
        .round(8)
        .astype(object)
        .where(d[cols].notna(), None)
        .values.tolist(),
        "events": window_events(history, d.index[0], d.index[-1]),
        "regimes": regimes(history),
        "comparison": compare_models(run, target, window, timing, backfill, d),
    }
