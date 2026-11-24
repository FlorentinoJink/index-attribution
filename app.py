"""Local A-share research server (Python 3.10+).

Install with python -m pip install -r requirements.txt, then run python app.py --open.
First launch collects public market data into data/market.sqlite. Run tests with
python -m unittest discover.
"""

import argparse
import csv
import io
import json
import logging
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import research
import sources
import support
import support_view

lock = threading.Lock()
logger = logging.getLogger(__name__)
status = {"running": False, "error": None, "support_error": None}


def start_refresh(only_support=False):
    with lock:
        if status["running"]:
            return
        status.update(running=True, error=None, support_error=None)

    def job():
        try:
            jobs = [(support.refresh, "support_error")]
            if not only_support:
                jobs.insert(0, (sources.refresh, "error"))
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                pending = {pool.submit(collect): key for collect, key in jobs}
                for future, key in pending.items():
                    try:
                        future.result()
                    except Exception as exc:
                        logger.exception("Data collection failed")
                        with lock:
                            status[key] = str(exc)
        finally:
            with lock:
                status["running"] = False

    threading.Thread(target=job, daemon=True).start()


def periodic():
    while True:
        now = sources.datetime.now(sources.CHINA)
        if now.weekday() < 5 and now.hour >= 18:
            try:
                run, _ = sources.inventory()
                due = run["cutoff"] < str(now.date())
            except ValueError:
                due = True
            if due:
                start_refresh()
            elif not support.recorded_on(str(now.date())):
                start_refresh(only_support=True)
        time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    def send_body(self, body, kind, status_code=200, filename=None):
        if not isinstance(body, bytes):
            body = body.encode()
        self.send_response(status_code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send_body(
            json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
            "application/json; charset=utf-8",
            code,
        )

    def valid_host(self):
        return self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")

    def do_POST(self):
        if (
            not self.valid_host()
            or self.headers.get("X-Research-Action") != self.path.rsplit("/", 1)[-1]
        ):
            return self.json({"error": "仅接受本机页面请求"}, 403)
        if self.path == "/api/event":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 8192:
                    raise ValueError("事件资料长度无效")
                data = json.loads(self.rfile.read(length))
                sources.add_event(
                    data["day"], data["title"], data["same_session"], data["url"]
                )
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                return self.json({"error": str(exc)}, 400)
            return self.json({"saved": True}, 201)
        if self.path != "/api/refresh":
            return self.json({"error": "未知接口"}, 404)
        start_refresh()
        self.json({"accepted": True}, 202)

    def do_GET(self):
        if not self.valid_host():
            return self.json({"error": "仅限本机"}, 403)
        try:
            self.route()
        except (ValueError, KeyError, OverflowError) as exc:
            self.json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            logger.exception("Request failed")
            self.json({"error": "数据读取失败，请查看本机服务日志"}, 500)

    def route(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        static = {
            "/": ("index.html", "text/html"),
            "/style.css": ("style.css", "text/css"),
            "/app.js": ("app.js", "text/javascript"),
        }
        if path in static:
            filename, mime = static[path]
            return self.send_body(
                (sources.ROOT / "web" / filename).read_bytes(), mime + "; charset=utf-8"
            )
        get = {k: v[-1] for k, v in parse_qs(parsed.query).items()}.get
        if path == "/support":
            if get("snapshot") is None:
                try:
                    support.snapshot()
                except ValueError:
                    with lock:
                        loading = status["running"]
                    reload_tag = (
                        '<meta http-equiv="refresh" content="3">' if loading else ""
                    )
                    message = (
                        "正在采集首份 ETF 行情，请稍候。"
                        if loading
                        else "ETF 行情尚未就绪，请返回首页点击「更新数据」重试。"
                    )
                    return self.send_body(
                        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                        + reload_tag
                        + '<link rel="stylesheet" href="/style.css"><title>承接观察</title>'
                        + '<main><div class="heading"><h1>承接观察</h1></div><p>'
                        + message
                        + '</p><a href="/">返回首页</a></main></html>',
                        "text/html; charset=utf-8",
                        503,
                    )
            return self.send_body(support_view.render(get), "text/html; charset=utf-8")
        if path == "/support.csv":
            snap = support.snapshot(int(get("snapshot")) if get("snapshot") else None)
            frame, _ = support.study(snap["id"])
            return self.send_body(
                frame.to_csv(float_format="%.6f").encode("utf-8-sig"),
                "text/csv",
                filename=f"etf-support-s{snap['id']}.csv",
            )
        try:
            run, series = sources.inventory(int(get("run")) if get("run") else None)
        except ValueError:
            if path != "/api/meta" or get("run") is not None:
                raise
            with lock:
                state = dict(status)
            return self.json({"run": None, "targets": sources.TARGETS, "status": state})
        if path == "/api/meta":
            with closing(sources.connect()) as db:
                snaps = [
                    dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id DESC")
                ]
            with lock:
                state = dict(status)
            for key, s in series.items():
                code, name, _, launch, source = sources.SPECS[key]
                s.update(code=code, name=name, launch=launch, provider=source)
            return self.json(
                {
                    "run": run,
                    "series": series,
                    "snapshots": snaps,
                    "targets": sources.TARGETS,
                    "names": research.LABELS,
                    "models": research.MODELS,
                    "plans": research.PLANS,
                    "status": state,
                }
            )
        rid = run["id"]
        if path in ("/api/series.csv", "/api/raw"):
            key = get("key", "csi500")
            if key not in sources.SPECS:
                raise ValueError("未知数据序列")
            if path == "/api/raw":
                ext = "csv" if key == "yen" else "json"
                return self.send_body(
                    sources.read_series(rid, key, True),
                    "application/octet-stream",
                    filename=f"{key}-s{rid}.{ext}",
                )
            stream = io.StringIO()
            w = csv.writer(stream)
            w.writerow(["observation_date", "level"])
            w.writerows(sources.read_series(rid, key))
            return self.send_body(
                stream.getvalue().encode("utf-8-sig"),
                "text/csv",
                filename=f"{key}-s{rid}.csv",
            )
        if path not in ("/api/report", "/api/report.csv"):
            return self.json({"error": "页面不存在"}, 404)
        args = (
            rid,
            get("target", "csi500"),
            get("model", "combined"),
            int(get("window", "252")),
            get("timing", "close"),
            get("backfill", "0") == "1",
            get("start"),
            get("end"),
        )
        if path == "/api/report.csv":
            _, frame = research.select(*args)
            return self.send_body(
                frame.to_csv(float_format="%.9f").encode("utf-8-sig"),
                "text/csv",
                filename=f"{args[1]}-{args[2]}-{args[3]}-{args[4]}-s{rid}.csv",
            )
        self.json(research.analyze(*args))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--port", type=int, default=8717)
    cli.add_argument(
        "--open", action="store_true", help="Open the local research page in a browser"
    )
    args = cli.parse_args()
    port = args.port
    url = f"http://127.0.0.1:{port}"
    if args.open:
        from urllib.error import URLError
        from urllib.request import urlopen

        try:
            with urlopen(url + "/api/meta", timeout=2) as response:
                existing = json.load(response)
            if (
                isinstance(existing, dict)
                and existing.get("targets") == sources.TARGETS
            ):
                webbrowser.open(url)
                raise SystemExit(0)
        except (URLError, ValueError):
            pass
    sources.init()
    support.init()
    http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        sources.inventory()
    except ValueError:
        start_refresh()
    else:
        try:
            support.snapshot()
        except ValueError:
            start_refresh(only_support=True)
    threading.Thread(target=periodic, daemon=True).start()
    print(f"Index attribution: http://127.0.0.1:{port}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
