import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

KEY = os.environ.get("STEAM_FINANCIAL_KEY", "")
APPS = {int(x) for x in os.environ.get("APP_IDS", "").split(",") if x.strip()}
POLL = int(os.environ.get("POLL_SECONDS", "120"))
DB = os.environ.get("DB_PATH", "/data/sales.db")
PORT = int(os.environ.get("PORT", "8080"))
BASE = os.environ.get("STEAM_API_BASE", "https://partner.steam-api.com/IPartnerFinancialsService/")
PT = ZoneInfo("America/Los_Angeles")
HTML = (Path(__file__).parent / "index.html").read_bytes()

lock = threading.Lock()
con = sqlite3.connect(DB, check_same_thread=False)
con.executescript("""
create table if not exists sales(
  date text, appid int, packageid int, country text, platform text, type text,
  gross_units real, returned_units real, net_units real, activated real,
  gross_usd real, returns_usd real, tax_usd real, net_usd real);
create index if not exists sales_date on sales(date);
create table if not exists names(kind text, id text, name text, primary key(kind, id));
create table if not exists meta(k text primary key, v text);
""")


def meta(k, v=None):
    with lock, con:
        if v is None:
            r = con.execute("select v from meta where k=?", (k,)).fetchone()
            return r[0] if r else ""
        con.execute("insert or replace into meta values(?,?)", (k, str(v)))


def call(method, **params):
    q = urllib.parse.urlencode({"key": KEY, **params})
    with urllib.request.urlopen(f"{BASE}{method}/v001/?{q}", timeout=60) as r:
        j = json.load(r)
    return j.get("response", j)


def num(x):
    return float(x or 0)


def fetch_date(d):
    rows, names, last = [], [], 0
    while True:
        p = call("GetDetailedSales", date=d, highwatermark_id=last)
        for r in p.get("results", []):
            if APPS and not ({r.get("appid"), r.get("primary_appid")} & APPS):
                continue
            rows.append((
                d.replace("/", "-"), r.get("appid"), r.get("packageid"),
                r.get("country_code"), r.get("platform"), r.get("line_item_type"),
                num(r.get("gross_units_sold")), num(r.get("gross_units_returned")),
                num(r.get("net_units_sold")), num(r.get("gross_units_activated")),
                num(r.get("gross_sales_usd")), num(r.get("gross_returns_usd")),
                num(r.get("net_tax_usd")), num(r.get("net_sales_usd")),
            ))
        for i in p.get("app_info", []):
            names.append(("app", str(i["appid"]), i["app_name"]))
        for i in p.get("country_info", []):
            names.append(("country", i["country_code"], i["country_name"]))
        for i in p.get("package_info", []):
            names.append(("package", str(i["packageid"]), i["package_name"]))
        mx = int(p.get("max_id") or last)
        if mx == last or not p.get("results"):
            return rows, names
        last = mx
        time.sleep(0.2)


def sync():
    hwm = meta("hwm") or "0"
    r = call("GetChangedDatesForPartner", highwatermark=hwm)
    for d in r.get("dates", []):
        rows, names = fetch_date(d)
        with lock, con:
            con.execute("delete from sales where date=?", (d.replace("/", "-"),))
            con.executemany("insert into sales values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.executemany("insert or replace into names values(?,?,?)", names)
        time.sleep(0.2)
    meta("hwm", r.get("result_highwatermark", hwm))


def loop():
    while True:
        try:
            sync()
            meta("ok", datetime.now().isoformat(timespec="seconds"))
            meta("err", "-")
        except Exception as e:
            meta("err", f"{type(e).__name__}: {e}")
        meta("checked", datetime.now().isoformat(timespec="seconds"))
        time.sleep(POLL)


def data(days):
    today = datetime.now(PT).date()
    iso = lambda d: d.isoformat()

    def tot(a, b):
        with lock:
            r = con.execute(
                "select coalesce(sum(net_units),0), coalesce(sum(net_usd),0), coalesce(sum(returned_units),0)"
                " from sales where date between ? and ?", (a, b)).fetchone()
        return {"units": r[0], "usd": r[1], "returned": r[2]}

    start = iso(today - timedelta(days=days - 1))
    with lock:
        daily = {r[0]: r[1:] for r in con.execute(
            "select date, sum(net_units), sum(net_usd) from sales where date>=? group by date", (start,))}
        countries = con.execute(
            "select coalesce(n.name, s.country), sum(s.net_units), sum(s.net_usd) from sales s"
            " left join names n on n.kind='country' and n.id=s.country"
            " where s.date>=? group by s.country order by 3 desc limit 10", (start,)).fetchall()
    series = []
    for i in range(days):
        d = iso(today - timedelta(days=days - 1 - i))
        u, usd = daily.get(d, (0, 0))
        series.append({"date": d, "units": u, "usd": usd})
    return {
        "today_pt": iso(today),
        "today": tot(iso(today), iso(today)),
        "yesterday": tot(iso(today - timedelta(days=1)), iso(today - timedelta(days=1))),
        "d7": tot(iso(today - timedelta(days=6)), iso(today)),
        "d30": tot(iso(today - timedelta(days=29)), iso(today)),
        "all": tot("0000-00-00", "9999-99-99"),
        "series": series,
        "countries": [{"name": c[0], "units": c[1], "usd": c[2]} for c in countries],
        "checked": meta("checked"),
        "ok": meta("ok"),
        "error": meta("err"),
        "poll": POLL,
    }


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/data":
            q = urllib.parse.parse_qs(u.query)
            days = max(1, min(int(q.get("days", ["30"])[0]), 1000))
            body, ctype = json.dumps(data(days)).encode(), "application/json"
        elif u.path == "/healthz":
            body, ctype = b"ok", "text/plain"
        elif u.path == "/":
            body, ctype = HTML, "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not KEY:
        raise SystemExit("STEAM_FINANCIAL_KEY not set")
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
