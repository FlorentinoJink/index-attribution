"""Collect, validate and version end-of-day index levels."""

import csv
import gzip
import hashlib
import io
import json
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("ATTRIBUTION_DB", ROOT / "data" / "market.sqlite"))
CHINA = timezone(timedelta(hours=8))
CSI = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
CNI = "https://www.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
BIS = "https://stats.bis.org/api/v1/data/BIS,WS_EER,1.0/D.N.B.JP"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"

# Code, Chinese name, history start requested, first official publication, provider.
SPECS = {
    "csi500": ("000905", "中证500", "2004-12-31", "2007-01-15", "中证指数"),
    "star50": ("000688", "科创50", "2019-12-31", "2020-07-22", "中证指数"),
    "chinext": ("399006", "创业板指", "2010-05-31", "2010-06-01", "国证指数"),
    "sse": ("000001", "上证指数", "2004-12-31", "1991-07-15", "中证指数"),
    "market": ("000300", "沪深300", "2004-12-31", "2005-04-08", "中证指数"),
    "bond": ("000012", "上证国债指数", "2004-12-31", "2003-01-02", "中证指数"),
    "dxy": ("DX-Y.NYB", "美元指数 DXY", "2004-12-01", None, "Yahoo Finance"),
    "yen": ("D.N.B.JP", "日元名义有效汇率", "2004-12-01", None, "国际清算银行 BIS"),
}
TARGETS = ["csi500", "star50", "chinext", "sse"]


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with closing(connect()) as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, collected TEXT NOT NULL, cutoff TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS payloads(hash TEXT PRIMARY KEY, raw BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS series(run INTEGER NOT NULL, key TEXT NOT NULL, url TEXT NOT NULL,
                hash TEXT NOT NULL, first TEXT NOT NULL, last TEXT NOT NULL, n INTEGER NOT NULL,
                skipped INTEGER NOT NULL, points BLOB NOT NULL, PRIMARY KEY(run,key));
            CREATE TABLE IF NOT EXISTS annotations(day TEXT NOT NULL, title TEXT NOT NULL,
                same_session INTEGER NOT NULL, url TEXT NOT NULL, added TEXT NOT NULL,
                PRIMARY KEY(day,title));
        """)


def fetch(key, cutoff):
    code, _, start, _, _ = SPECS[key]
    if key == "yen":
        url, params = BIS, {"startPeriod": start, "endPeriod": cutoff, "format": "csv"}
    elif key == "dxy":
        from zoneinfo import ZoneInfo

        local_now = datetime.now(ZoneInfo("America/New_York"))
        last_closed = (
            local_now.date()
            if local_now.hour >= 18
            else local_now.date() - timedelta(days=1)
        )
        cutoff = min(cutoff, str(last_closed))

        def epoch(d):
            return int(
                datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()
            )

        url, params = (
            YAHOO,
            {
                "period1": epoch(start),
                "period2": epoch(str(date.fromisoformat(cutoff) + timedelta(days=1))),
                "interval": "1d",
            },
        )
    elif key == "chinext":
        url, params = (
            CNI,
            {
                "indexCode": code,
                "startDate": start,
                "endDate": cutoff,
                "frequency": "day",
            },
        )
    else:
        url, params = (
            CSI,
            {
                "indexCode": code,
                "startDate": start.replace("-", ""),
                "endDate": cutoff.replace("-", ""),
            },
        )
    for attempt in range(3):
        try:
            r = requests.get(
                url,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/csv" if key == "yen" else "application/json",
                },
                timeout=(7, 30),
            )
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    if key == "yen":
        items = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
        if not items or any(
            a["REF_AREA"] != "JP" or a["EER_TYPE"] != "N" or a["EER_BASKET"] != "B"
            for a in items
        ):
            raise ValueError("BIS 序列标识不符")
        rows = [(a["TIME_PERIOD"], a["OBS_VALUE"]) for a in items]
    elif key == "dxy":
        body = r.json()["chart"]["result"][0]
        if body["meta"]["symbol"] != code:
            raise ValueError("美元指数标识不符")
        # These are New York session dates. Daily timestamps are provided in UTC.
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(body["meta"]["exchangeTimezoneName"])
        rows = [
            (str(datetime.fromtimestamp(ts, zone).date()), v)
            for ts, v in zip(
                body["timestamp"], body["indicators"]["quote"][0]["close"], strict=True
            )
        ]
    elif key == "chinext":
        body = r.json()["data"]
        if body["indexCode"] != code:
            raise ValueError("创业板指标识不符")
        i, j = body["item"].index("timestamp"), body["item"].index("close")
        rows = [(a[i], a[j]) for a in body["data"]]
    else:
        items = r.json()["data"]
        if not items or any(a["indexCode"] != code for a in items):
            raise ValueError(f"{code} 标识不符")
        rows = [
            (
                datetime.strptime(a["tradeDate"], "%Y%m%d")
                .replace(tzinfo=CHINA)
                .date()
                .isoformat(),
                a["close"],
            )
            for a in items
        ]
    points = {}
    for day, raw in rows:
        dt = date.fromisoformat(day)
        if raw is None or dt.weekday() >= 5 or not start <= day <= cutoff:
            continue
        level = float(raw)
        if not math.isfinite(level):
            continue
        if level <= 0:
            raise ValueError(f"{code} 非正点位 {day}")
        if day in points and points[day] != level:
            raise ValueError(f"{code} 日期冲突 {day}")
        points[day] = level
    ordered = sorted(points.items())
    if (
        len(ordered) < 500
        or (date.fromisoformat(ordered[0][0]) - date.fromisoformat(start)).days > 14
    ):
        raise ValueError(f"{code} 历史覆盖不足")
    return key, r.url, r.content, ordered, len(rows) - len(ordered)


def refresh():
    now = datetime.now(CHINA)
    cutoff = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    # A failed or incomplete download never replaces a working snapshot.
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(lambda k: fetch(k, str(cutoff)), SPECS))
    with closing(connect()) as db, db:
        prev = {
            r["key"]: r
            for r in db.execute(
                "SELECT * FROM series WHERE run=(SELECT max(id) FROM runs)"
            )
        }
        run = db.execute(
            "INSERT INTO runs(collected,cutoff) VALUES(?,?)",
            (now.isoformat(timespec="seconds"), str(cutoff)),
        ).lastrowid
        for key, url, raw, points, skipped in batches:
            first, last = points[0][0], points[-1][0]
            if key in prev and (
                last < prev[key]["last"] or len(points) < prev[key]["n"] * 0.99
            ):
                raise ValueError(f"{key} 数据覆盖倒退")
            digest = hashlib.sha256(raw).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO payloads VALUES(?,?)",
                (digest, gzip.compress(raw)),
            )
            normalized = gzip.compress(
                json.dumps(points, separators=(",", ":")).encode()
            )
            db.execute(
                "INSERT INTO series VALUES(?,?,?,?,?,?,?,?,?)",
                (run, key, url, digest, first, last, len(points), skipped, normalized),
            )
    return run


def inventory(run=None):
    with closing(connect()) as db:
        row = db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
            if run is None
            else "SELECT * FROM runs WHERE id=?",
            () if run is None else (run,),
        ).fetchone()
        if row is None:
            raise ValueError("尚无数据快照")
        series = {
            r["key"]: dict(r)
            for r in db.execute(
                "SELECT run,key,url,hash,first,last,n,skipped FROM series WHERE run=?",
                (row["id"],),
            )
        }
        return dict(row), series


def add_event(day, title, same_session, url):
    from urllib.parse import urlsplit

    date.fromisoformat(day)
    title = title.strip()
    parts = urlsplit(url)
    if (
        not title
        or len(title) > 90
        or parts.scheme not in ("https", "http")
        or not parts.hostname
        or len(url) > 1500
    ):
        raise ValueError("请填写事件标题和可核验的 HTTP(S) 来源链接")
    if (
        day > str(datetime.now(CHINA).date())
        or day < "2005-01-01"
        or type(same_session) is not bool
    ):
        raise ValueError("请使用已发生的事件日期及有效的观察口径")
    with closing(connect()) as db, db:
        try:
            db.execute(
                "INSERT INTO annotations VALUES(?,?,?,?,?)",
                (
                    day,
                    title,
                    int(same_session),
                    url,
                    datetime.now(CHINA).isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该日期和标题的事件已经登记") from exc


def annotations():
    with closing(connect()) as db:
        return [
            (r["day"], r["title"], "用户登记", bool(r["same_session"]), r["url"])
            for r in db.execute("SELECT * FROM annotations ORDER BY day")
        ]


def read_series(run, key, raw=False):
    with closing(connect()) as db:
        col = "p.raw" if raw else "s.points"
        row = db.execute(
            f"SELECT {col} FROM payloads p JOIN series s ON s.hash=p.hash WHERE s.run=? AND s.key=?",
            (run, key),
        ).fetchone()
        if row is None:
            raise ValueError("数据集不存在")
        data = gzip.decompress(row[0])
        return data if raw else json.loads(data)


if __name__ == "__main__":
    init()
    run = refresh()
    print(f"Snapshot {run}")
    for key, s in inventory(run)[1].items():
        print(key, s["first"], s["last"], s["n"])
