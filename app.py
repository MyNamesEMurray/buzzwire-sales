import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

KEY = os.environ.get("STEAM_FINANCIAL_KEY", "")
APPS = {int(x) for x in os.environ.get("APP_IDS", "").split(",") if x.strip()}
POLL = int(os.environ.get("POLL_SECONDS", "120"))
PLAYER_POLL = int(os.environ.get("PLAYER_POLL_SECONDS", "300"))
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
create table if not exists players(ts int, count int);
create index if not exists players_ts on players(ts);
create table if not exists wishlist(date text primary key, adds real, deletes real, purchases real, gifts real);
""")


def meta(k, v=None):
    with lock, con:
        if v is None:
            r = con.execute("select v from meta where k=?", (k,)).fetchone()
            return r[0] if r else ""
        con.execute("insert or replace into meta values(?,?)", (k, str(v)))


def getj(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


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


def app_id():
    if APPS:
        return min(APPS)
    with lock:
        r = con.execute("select distinct appid from sales where appid is not null limit 2").fetchall()
    return r[0][0] if len(r) == 1 else None


def players(aid):
    c = getj(f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={aid}")["response"]["player_count"]
    with lock, con:
        con.execute("insert into players values(?,?)", (int(time.time()), int(c)))


def reviews(aid):
    q = urllib.parse.urlencode({"input_json": json.dumps({"appid": aid, "num_per_page": 1, "languages": ["all"]})})
    j = getj(f"https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/?{q}")["response"]["query_summary"]
    meta("reviews", json.dumps(j))


def wishlist(aid):
    with lock:
        have = con.execute("select count(*) from wishlist").fetchone()[0]
    today = datetime.now(timezone.utc).date()
    err, got = None, 0
    for i in range(1, (3 if have else 30) + 1):
        d = (today - timedelta(days=i)).isoformat()
        try:
            s = call("GetAppWishlistReporting", appid=aid, date=d).get("wishlist_summary", {})
        except Exception as e:
            err = e
            continue
        with lock, con:
            con.execute("insert or replace into wishlist values(?,?,?,?,?)", (
                d, num(s.get("wishlist_adds")), num(s.get("wishlist_deletes")),
                num(s.get("wishlist_purchases")), num(s.get("wishlist_gifts"))))
        got += 1
        time.sleep(0.3)
    if err and not got:
        raise err


def attempt(errs, name, f, *a):
    try:
        f(*a)
    except Exception as e:
        errs.append(f"{name}: {type(e).__name__}: {e}")


def aux_tick(n):
    aid = app_id()
    if not aid:
        meta("aerr", "app id unknown: set APP_IDS")
        return False
    errs = []
    attempt(errs, "players", players, aid)
    if n % 3 == 0:
        attempt(errs, "reviews", reviews, aid)
    if n % 6 == 0:
        attempt(errs, "wishlist", wishlist, aid)
    meta("aerr", "; ".join(errs) or "-")
    return True


def aux():
    n = 0
    while True:
        if aux_tick(n):
            n += 1
            time.sleep(PLAYER_POLL)
        else:
            time.sleep(30)


def data(days):
    today = datetime.now(PT).date()
    iso = lambda d: d.isoformat()
    if days == "all":
        with lock:
            first = con.execute("select min(date) from sales").fetchone()[0]
        days = max(1, (today - datetime.strptime(first, "%Y-%m-%d").date()).days + 1) if first else 1

    def tot(a, b):
        with lock:
            r = con.execute(
                "select coalesce(sum(net_units),0), coalesce(sum(net_usd),0), coalesce(sum(returned_units),0)"
                " from sales where date between ? and ?", (a, b)).fetchone()
        return {"units": r[0], "usd": r[1], "returned": r[2]}

    start = iso(today - timedelta(days=days - 1))
    now = int(time.time())
    with lock:
        daily = {r[0]: r[1:] for r in con.execute(
            "select date, sum(net_units), sum(net_usd) from sales where date>=? group by date", (start,))}
        countries = con.execute(
            "select coalesce(n.name, s.country), sum(s.net_units), sum(s.net_usd) from sales s"
            " left join names n on n.kind='country' and n.id=s.country"
            " where s.date>=? group by s.country order by 3 desc limit 10", (start,)).fetchall()
        cur = con.execute("select count from players order by ts desc limit 1").fetchone()
        peak = con.execute("select max(count) from players where ts>?", (now - 86400,)).fetchone()[0]
        bucket = 3600 if days <= 7 else 86400
        pser = con.execute(
            "select (ts/?)*?, max(count) from players where ts>? group by 1 order by 1",
            (bucket, bucket, now - max(days, 2) * 86400)).fetchall()
        wl = con.execute(
            "select date, adds, deletes, purchases, gifts from wishlist where date>=? order by date desc", (start,)).fetchall()
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
        "players": {
            "now": cur[0] if cur else None,
            "peak24h": peak,
            "bucket": bucket,
            "series": [{"t": t, "c": c} for t, c in pser],
        },
        "days": days,
        "reviews": json.loads(meta("reviews") or "null"),
        "wishlist": [{"date": r[0], "adds": r[1], "deletes": r[2], "purchases": r[3], "gifts": r[4]} for r in wl],
        "checked": meta("checked"),
        "ok": meta("ok"),
        "error": meta("err"),
        "aux_error": meta("aerr"),
        "poll": POLL,
    }


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/data":
            q = urllib.parse.parse_qs(u.query)
            raw = q.get("days", ["30"])[0]
            days = "all" if raw == "all" else max(1, min(int(raw), 1000))
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
    threading.Thread(target=aux, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
