"""Identify synchronous ETF turnover and rebounds in CSI 300 pressure sessions."""

import gzip
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd
import requests

import sources

INDEX = "000300"
ETFS = {"510300": "华泰柏瑞", "510310": "易方达", "510330": "华夏"}
CODES = [INDEX, *ETFS]
START = "2012-01-01"
FIELDS = "date,open,high,low,close,volume,amount"
RULE = "v1"


def init():
    with closing(sources.connect()) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS support_runs(
                id INTEGER PRIMARY KEY, collected TEXT NOT NULL, cutoff TEXT NOT NULL,
                fingerprint TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS support_feeds(
                run INTEGER NOT NULL, code TEXT NOT NULL, url TEXT NOT NULL,
                hash TEXT NOT NULL, first TEXT NOT NULL, last TEXT NOT NULL,
                n INTEGER NOT NULL, PRIMARY KEY(run,code));
            CREATE TABLE IF NOT EXISTS support_calls(
                rule TEXT NOT NULL, day TEXT NOT NULL, run INTEGER NOT NULL, marked TEXT NOT NULL,
                watch INTEGER NOT NULL, next_day TEXT, hit INTEGER, settled_run INTEGER,
                PRIMARY KEY(rule,day));
        """)


def bars(raw, code, cutoff):
    doc = json.loads(raw)
    if doc["code"] != code:
        raise ValueError(f"{code} 行情标识不符")
    rows = []
    for stamp, op, high, low, close, vol, amount in doc["kline"]:
        day = date(stamp // 10000, stamp // 100 % 100, stamp % 100).isoformat()
        if not START <= day <= cutoff:
            continue
        if rows and rows[-1][0] >= day:
            raise ValueError(f"{code} 日期乱序或重复")
        if not all(math.isfinite(v) for v in [op, high, low, close, vol, amount]):
            raise ValueError(f"{code} 行情数值无效")
        if not (
            0 < low <= min(op, close) <= max(op, close) <= high
            and vol >= 0
            and amount >= 0
        ):
            raise ValueError(f"{code} 日线字段无效")
        rows.append((day, op, low, close, amount))
    if not rows:
        raise ValueError(f"{code} 没有可用日线")
    return rows


def collect():
    now = datetime.now(sources.CHINA)
    cutoff = str(now.date() if now.hour >= 16 else now.date() - timedelta(days=1))

    def fetch(code):
        resp = requests.get(
            f"https://yunhq.sse.com.cn:32042/v1/sh1/dayk/{code}",
            params={"select": FIELDS, "begin": 0, "end": -1},
            headers={"Referer": "https://www.sse.com.cn/", "User-Agent": "Mozilla/5.0"},
            timeout=(8, 25),
        )
        resp.raise_for_status()
        raw = resp.content
        rows = bars(raw, code, cutoff)
        return (
            code,
            resp.url,
            raw,
            hashlib.sha256(raw).hexdigest(),
            rows[0][0],
            rows[-1][0],
            len(rows),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        feeds = list(pool.map(fetch, CODES))
    key = hashlib.sha256((cutoff + "".join(f[3] for f in feeds)).encode()).hexdigest()
    with closing(sources.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        same = db.execute(
            "SELECT id FROM support_runs WHERE fingerprint=?", (key,)
        ).fetchone()
        if same:
            return same["id"]
        prev = {
            r["code"]: r
            for r in db.execute(
                "SELECT * FROM support_feeds WHERE run=(SELECT max(id) FROM support_runs)"
            )
        }
        run = db.execute(
            "INSERT INTO support_runs(collected,cutoff,fingerprint) VALUES(?,?,?)",
            (now.isoformat(timespec="seconds"), cutoff, key),
        ).lastrowid
        for code, url, raw, digest, first, last, n in feeds:
            if code in prev and (
                first > prev[code]["first"]
                or last < prev[code]["last"]
                or n < prev[code]["n"]
            ):
                raise ValueError(f"{code} 行情覆盖倒退")
            db.execute(
                "INSERT OR IGNORE INTO payloads VALUES(?,?)",
                (digest, gzip.compress(raw)),
            )
            db.execute(
                "INSERT INTO support_feeds VALUES(?,?,?,?,?,?,?)",
                (run, code, url, digest, first, last, n),
            )
    return run


def snapshot(run=None):
    with closing(sources.connect()) as db:
        row = (
            db.execute("SELECT * FROM support_runs ORDER BY id DESC LIMIT 1")
            if run is None
            else db.execute("SELECT * FROM support_runs WHERE id=?", (run,))
        ).fetchone()
        if row is None:
            raise ValueError("尚无对应的 ETF 快照")
        return dict(row)


def indicators(frames):
    index = frames[INDEX]
    prev = index.close.shift()
    day = pd.DataFrame(index=index.index)
    day["low_ret"] = (index.low / prev - 1) * 100
    day["close_ret"] = (index.close / prev - 1) * 100
    day["drawdown20"] = (index.close / index.close.rolling(20).max() - 1) * 100
    for code in ETFS:
        amount = frames[code].amount.reindex(index.index)
        amount = amount.where(amount > 0)
        day[code] = amount / amount.shift().rolling(60, min_periods=60).median()
    complete = day.notna().all(axis=1)
    day["surge"] = day[list(ETFS)].ge(2).sum(axis=1)
    day["pressure"] = day.low_ret <= -1
    day["bounce"] = day.close_ret >= day.low_ret / 2
    day["candidate"] = day.pressure & (day.surge >= 2) & day.bounce
    quiet = (
        day.candidate.where(complete)
        .astype("float64")
        .rolling(5, min_periods=5)
        .sum()
        .eq(0)
    )
    day["quiet"] = quiet
    day["setup_pool"] = quiet & (day.drawdown20 <= -3)
    day["watch"] = day.setup_pool & (day[list(ETFS)].ge(1.5).sum(axis=1) >= 2)
    # Shift on the full exchange calendar so a missing session cannot act as tomorrow.
    day["next_event"] = day.candidate.astype(float).where(complete).shift(-1)
    fund = frames["510300"].reindex(index.index)
    day["next_oc"] = (
        ((fund.close / fund.open - 1) * 100).where(fund.amount > 0).shift(-1)
    )
    day["next_day"] = pd.Series(index.index, index=index.index).shift(-1)
    day = day[complete].copy()
    day["controls"] = 0
    day["control_bounces"] = 0
    # Match severity against earlier ordinary-volume sessions, never against future returns.
    for i in range(len(day)):
        if not day.pressure.iat[i]:
            continue
        at = day.index[i]
        j = index.index.get_loc(at)
        hist = day.iloc[:i].loc[index.index[max(0, j - 504)] :]
        controls = hist[
            hist.pressure
            & (hist.surge < 2)
            & ((hist.low_ret - day.low_ret.iat[i]).abs() <= 0.35)
        ]
        day.at[at, "controls"] = len(controls)
        day.at[at, "control_bounces"] = int(controls.bounce.sum())
    return day


@lru_cache(maxsize=4)
def study(run):
    meta = snapshot(run)
    frames, feeds = {}, []
    with closing(sources.connect()) as db:
        for row in db.execute(
            "SELECT f.*, p.raw FROM support_feeds f JOIN payloads p USING(hash) WHERE run=? ORDER BY code",
            (run,),
        ):
            entry = dict(row)
            raw = gzip.decompress(entry.pop("raw"))
            if hashlib.sha256(raw).hexdigest() != entry["hash"]:
                raise ValueError("ETF 快照校验失败")
            code = entry["code"]
            rows = bars(raw, code, meta["cutoff"])
            frames[code] = pd.DataFrame(
                rows, columns=["date", "open", "low", "close", "amount"]
            ).set_index("date")
            feeds.append(entry)
    return indicators(frames), feeds


def record_live(run):
    frame, _ = study(run)
    now = datetime.now(sources.CHINA)
    last = frame.index[-1]
    with closing(sources.connect()) as db, db:
        if last == str(now.date()) and now.hour >= 16:
            db.execute(
                "INSERT OR IGNORE INTO support_calls(rule,day,run,marked,watch) VALUES(?,?,?,?,?)",
                (
                    RULE,
                    last,
                    run,
                    now.isoformat(timespec="seconds"),
                    int(frame.watch.iloc[-1]),
                ),
            )
        pending = db.execute(
            "SELECT day FROM support_calls WHERE rule=? AND hit IS NULL AND run<?",
            (RULE, run),
        ).fetchall()
        for (day,) in pending:
            if day in frame.index and pd.notna(frame.at[day, "next_event"]):
                db.execute(
                    "UPDATE support_calls SET next_day=?, hit=?, settled_run=? WHERE rule=? AND day=? AND hit IS NULL",
                    (
                        frame.at[day, "next_day"],
                        int(frame.at[day, "next_event"]),
                        run,
                        RULE,
                        day,
                    ),
                )


def refresh():
    run = collect()
    record_live(run)
    return run


def recorded_on(day):
    with closing(sources.connect()) as db:
        return (
            db.execute(
                "SELECT 1 FROM support_calls WHERE rule=? AND day=?", (RULE, day)
            ).fetchone()
            is not None
        )


def forward_summary(run):
    with closing(sources.connect()) as db:
        row = db.execute(
            """SELECT count(*) AS days, coalesce(sum(watch),0) AS calls,
            count(CASE WHEN watch=1 AND settled_run<=? THEN 1 END) AS settled,
            count(CASE WHEN watch=1 AND hit=1 AND settled_run<=? THEN 1 END) AS correct
            FROM support_calls WHERE rule=? AND run<=?""",
            (run, run, RULE, run),
        ).fetchone()
        return dict(row)
