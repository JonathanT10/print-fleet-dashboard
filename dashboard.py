"""Generate a self-contained HTML dashboard from the fleet database.

    python dashboard.py                          # fleet.db -> fleet.html
    python dashboard.py --db fleet.db --out /var/www/fleet.html
    python dashboard.py --serve 8080             # live-regenerating local server

The output is one static HTML file with no external dependencies - drop it on
any file share or intranet site. Cron the collector, cron this, done.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import fleetdb

STALE_HOURS = 48          # last contact older than this => offline
TREND_DAYS = 30           # fleet volume window
SPARK_DAYS = 14           # per-device sparkline window
LOW_SUPPLY = 0.20         # "supplies low" threshold
CRIT_SUPPLY = 0.10


# --------------------------------------------------------------------------- #
# Data shaping
# --------------------------------------------------------------------------- #

def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def daily_page_deltas(conn, device_id, days, now):
    """Per-day printed pages for one device: delta of the day's max page_count."""
    since = (now - timedelta(days=days + 1)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT substr(ts, 1, 10) AS day, MAX(page_count) AS pages
           FROM snapshots
           WHERE device_id = ? AND page_count IS NOT NULL AND substr(ts,1,10) >= ?
           GROUP BY day ORDER BY day""",
        (device_id, since),
    ).fetchall()
    out, prev = {}, None
    for r in rows:
        if prev is not None and r["pages"] is not None:
            out[r["day"]] = max(0, r["pages"] - prev)
        prev = r["pages"] if r["pages"] is not None else prev
    return out  # {'YYYY-MM-DD': pages}


def collect(conn):
    now = datetime.now(timezone.utc)
    devices = conn.execute("SELECT * FROM devices ORDER BY name, ip").fetchall()

    day_axis = [(now - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(TREND_DAYS - 1, -1, -1)]
    fleet_daily = {d: 0 for d in day_axis}

    dev_rows, attention = [], []
    online = supplies_low = 0

    for d in devices:
        snap = conn.execute(
            "SELECT * FROM snapshots WHERE device_id = ? ORDER BY ts DESC LIMIT 1",
            (d["id"],),
        ).fetchone()
        supplies = []
        status, detail, pages = "offline", "Never polled", None
        last_seen = d["last_seen"]  # last *successful* contact, not last attempt
        if snap:
            pages = snap["page_count"]
            age_h = (now - parse_ts(snap["ts"])).total_seconds() / 3600
            if not snap["reachable"] or snap["status"] == "offline":
                status, detail = "offline", snap["detail"] or "No SNMP response"
            elif age_h > STALE_HOURS:
                status, detail = "offline", f"No data for {int(age_h // 24)}d"
            else:
                status, detail = snap["status"] or "ok", snap["detail"] or ""
                supplies = [dict(r) for r in conn.execute(
                    "SELECT * FROM supplies WHERE snapshot_id = ? ORDER BY slot",
                    (snap["id"],),
                ).fetchall()]

        if status != "offline":
            online += 1
        for s in supplies:
            if s["max_capacity"] and s["level"] is not None and s["level"] >= 0:
                if s["level"] / s["max_capacity"] < LOW_SUPPLY:
                    supplies_low += 1
        if status in ("warning", "error", "offline"):
            attention.append({"name": d["name"] or d["ip"], "ip": d["ip"],
                              "status": status, "detail": detail})

        deltas = daily_page_deltas(conn, d["id"], TREND_DAYS, now)
        for day, v in deltas.items():
            if day in fleet_daily:
                fleet_daily[day] += v
        spark = [deltas.get(day, 0) for day in day_axis[-SPARK_DAYS:]]

        dev_rows.append({
            "name": d["name"] or d["ip"], "ip": d["ip"],
            "model": d["model"] or "Unknown model", "serial": d["serial"] or "",
            "status": status, "detail": detail,
            "pages": pages, "last_seen": last_seen,
            "supplies": [
                {"desc": s["description"] or f"Slot {s['slot']}",
                 "pct": (round(100 * s["level"] / s["max_capacity"])
                         if s["max_capacity"] and s["level"] is not None
                         and s["level"] >= 0 else None)}
                for s in supplies
            ],
            "spark": spark,
        })

    week = sum(fleet_daily[d] for d in day_axis[-7:])
    prev_week = sum(fleet_daily[d] for d in day_axis[-14:-7])
    delta_pct = (round(100 * (week - prev_week) / prev_week) if prev_week else None)

    status_rank = {"error": 0, "offline": 1, "warning": 2}
    attention.sort(key=lambda a: status_rank.get(a["status"], 3))

    return {
        "generated": now.strftime("%Y-%m-%d %H:%M UTC"),
        "devices": dev_rows,
        "attention": attention,
        "kpi": {"online": online, "total": len(devices),
                "attention": len(attention), "week": week,
                "delta_pct": delta_pct, "supplies_low": supplies_low},
        "trend": {"days": day_axis, "pages": [fleet_daily[d] for d in day_axis]},
    }


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def render(data: dict) -> str:
    payload = json.dumps(data)
    return HTML_TEMPLATE.replace("__PAYLOAD__", payload)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Print Fleet</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --accent: #2a78d6; --accent-deep: #1c5cab;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --spark: #c3c2b7;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --accent-deep: #86b6ef;
    --spark: #52514e;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --accent: #3987e5; --accent-deep: #86b6ef;
  --spark: #52514e;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 24px 20px 48px; }
header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
header h1 { font-size: 20px; font-weight: 650; margin: 0; }
header .sub { color: var(--ink-2); font-size: 13px; }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 18px; }
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px;
}
.card .label { color: var(--ink-2); font-size: 12.5px; }
.card .value { font-size: 30px; font-weight: 600; margin-top: 2px; }
.card .note { font-size: 12px; color: var(--muted); margin-top: 2px; }
.card .delta-up   { color: #006300; }
:root[data-theme="dark"] .card .delta-up { color: #0ca30c; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .card .delta-up { color: #0ca30c; } }

.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 18px; }
.panel h2 { font-size: 14px; font-weight: 650; margin: 0 0 10px; }
.panel h2 .h2sub { color: var(--muted); font-weight: 400; font-size: 12.5px; }

.attn { display: flex; flex-direction: column; gap: 6px; }
.attn-row { display: flex; align-items: center; gap: 10px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 8px; }
.attn-row .who { font-weight: 600; }
.attn-row .what { color: var(--ink-2); }
.attn-row .ip { color: var(--muted); font-size: 12px; margin-left: auto; }

.badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--ink-2); white-space: nowrap; }
.badge svg { flex: 0 0 auto; }

.chart-wrap { position: relative; }
#trend { width: 100%; height: 220px; display: block; }
.tooltip {
  position: absolute; pointer-events: none; display: none;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 6px 10px; font-size: 12.5px; box-shadow: 0 2px 8px rgba(0,0,0,.12);
  white-space: nowrap; z-index: 5;
}
.tooltip .tt-day { color: var(--ink-2); }
.tooltip .tt-val { font-weight: 600; }

details.tableview { margin-top: 8px; }
details.tableview summary { cursor: pointer; color: var(--ink-2); font-size: 12.5px; }
details.tableview table { margin-top: 8px; border-collapse: collapse; font-size: 12.5px; }
details.tableview td, details.tableview th { padding: 3px 12px 3px 0; text-align: right; font-variant-numeric: tabular-nums; }
details.tableview th { color: var(--muted); font-weight: 500; text-align: right; }
details.tableview td:first-child, details.tableview th:first-child { text-align: left; }

table.fleet { width: 100%; border-collapse: collapse; }
table.fleet th {
  text-align: left; font-size: 12px; font-weight: 550; color: var(--muted);
  padding: 6px 10px; border-bottom: 1px solid var(--grid);
}
table.fleet td { padding: 10px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
table.fleet tr:last-child td { border-bottom: none; }
td.name .nm { font-weight: 600; }
td.name .mdl { color: var(--ink-2); font-size: 12.5px; }
td.pages { text-align: right; font-variant-numeric: tabular-nums; }
th.pages { text-align: right; }
td.seen { color: var(--ink-2); font-size: 12.5px; white-space: nowrap; }

.meters { display: flex; flex-direction: column; gap: 5px; min-width: 150px; }
.meter { display: grid; grid-template-columns: 16px 1fr 34px; gap: 7px; align-items: center; }
.meter .mlabel { font-size: 11.5px; color: var(--ink-2); }
.meter .mpct { font-size: 11.5px; color: var(--ink-2); text-align: right; font-variant-numeric: tabular-nums; }
.meter .track { height: 7px; border-radius: 4px; overflow: hidden; }
.meter .fill { height: 100%; border-radius: 4px; }

.spark svg { display: block; }
footer { color: var(--muted); font-size: 12px; margin-top: 10px; }
footer a { color: inherit; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Print Fleet</h1>
    <span class="sub" id="generated"></span>
  </header>

  <div class="cards" id="cards"></div>

  <div class="panel" id="attnPanel" hidden>
    <h2>Needs attention</h2>
    <div class="attn" id="attn"></div>
  </div>

  <div class="panel">
    <h2>Pages printed per day <span class="h2sub">&middot; fleet total, last 30 days</span></h2>
    <div class="chart-wrap">
      <svg id="trend" role="img" aria-label="Fleet pages printed per day, last 30 days"></svg>
      <div class="tooltip" id="tt"><span class="tt-day"></span> &middot; <span class="tt-val"></span></div>
    </div>
    <details class="tableview"><summary>View as table</summary><div id="trendTable"></div></details>
  </div>

  <div class="panel">
    <h2>Devices</h2>
    <div style="overflow-x:auto">
    <table class="fleet">
      <thead><tr>
        <th>Device</th><th>Status</th><th>Supplies</th>
        <th class="pages">Lifetime pages</th><th>14-day activity</th><th>Last seen</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
    </div>
  </div>

  <footer>Generated by <a href="https://github.com/JonathanT10/print-fleet-dashboard">print-fleet-dashboard</a>.</footer>
</div>

<script>
const DATA = __PAYLOAD__;

const fmt = n => n == null ? "&mdash;" : n.toLocaleString("en-US");
const compact = n => n == null ? "&mdash;"
  : n >= 1e6 ? (n / 1e6).toFixed(1) + "M"
  : n >= 1e4 ? Math.round(n / 1e3) + "K"
  : n.toLocaleString("en-US");

const STATUS = {
  ok:      { color: "var(--good)",     label: "OK",      icon: "check" },
  warning: { color: "var(--warning)",  label: "Warning", icon: "alert" },
  error:   { color: "var(--critical)", label: "Error",   icon: "cross" },
  offline: { color: "var(--serious)",  label: "Offline", icon: "off"   },
};
function icon(kind, color) {
  const p = {
    check: '<path d="M3 8.5l3 3 7-7" fill="none" stroke="CLR" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    alert: '<path d="M8 1.5L15 14H1z" fill="CLR"/><rect x="7.2" y="6" width="1.6" height="4" rx="0.8" fill="var(--surface)"/><circle cx="8" cy="12" r="0.9" fill="var(--surface)"/>',
    cross: '<circle cx="8" cy="8" r="7" fill="CLR"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="var(--surface)" stroke-width="1.8" stroke-linecap="round"/>',
    off:   '<circle cx="8" cy="8" r="6.2" fill="none" stroke="CLR" stroke-width="2"/><rect x="7.1" y="4" width="1.8" height="5" rx="0.9" fill="CLR"/>',
  }[kind].replaceAll("CLR", color);
  return `<svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true">${p}</svg>`;
}
const badge = st => {
  const s = STATUS[st] || STATUS.ok;
  return `<span class="badge">${icon(s.icon, s.color)}${s.label}</span>`;
};

/* ---- KPI cards ---- */
document.getElementById("generated").textContent =
  "Updated " + DATA.generated + " \u00B7 " + DATA.kpi.total + " devices";
const k = DATA.kpi;
const deltaHtml = k.delta_pct == null ? ""
  : `<div class="note ${k.delta_pct >= 0 ? "delta-up" : ""}">${k.delta_pct >= 0 ? "+" : ""}${k.delta_pct}% vs prior week</div>`;
document.getElementById("cards").innerHTML = `
  <div class="card"><div class="label">Devices online</div>
    <div class="value">${k.online}<span style="color:var(--muted);font-size:18px"> / ${k.total}</span></div></div>
  <div class="card"><div class="label">Needs attention</div>
    <div class="value">${k.attention}</div>
    <div class="note">${k.attention ? "see list below" : "all clear"}</div></div>
  <div class="card"><div class="label">Pages, last 7 days</div>
    <div class="value">${compact(k.week)}</div>${deltaHtml}</div>
  <div class="card"><div class="label">Supplies below 20%</div>
    <div class="value">${k.supplies_low}</div></div>`;

/* ---- Attention list ---- */
if (DATA.attention.length) {
  document.getElementById("attnPanel").hidden = false;
  document.getElementById("attn").innerHTML = DATA.attention.map(a => `
    <div class="attn-row">${badge(a.status)}
      <span class="who">${a.name}</span>
      <span class="what">${a.detail || ""}</span>
      <span class="ip">${a.ip}</span></div>`).join("");
}

/* ---- Trend chart: single series line + 10% area wash, hover crosshair ---- */
(function trend() {
  const svg = document.getElementById("trend");
  const days = DATA.trend.days, vals = DATA.trend.pages;
  const W = svg.clientWidth || 1000, H = 220;
  const M = { t: 12, r: 14, b: 24, l: 46 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const maxV = Math.max(1, ...vals);
  // clean tick ceiling
  const pow = Math.pow(10, Math.floor(Math.log10(maxV)));
  const top = Math.ceil(maxV / pow) * pow;
  const x = i => M.l + (W - M.l - M.r) * (days.length === 1 ? 0.5 : i / (days.length - 1));
  const y = v => M.t + (H - M.t - M.b) * (1 - v / top);

  let g = "";
  const ticks = 4;
  for (let t = 0; t <= ticks; t++) {
    const v = top * t / ticks, yy = y(v);
    g += `<line x1="${M.l}" y1="${yy}" x2="${W - M.r}" y2="${yy}" stroke="var(--grid)" stroke-width="1"/>`;
    g += `<text x="${M.l - 8}" y="${yy + 4}" text-anchor="end" font-size="11" fill="var(--muted)" style="font-variant-numeric:tabular-nums">${compact(v)}</text>`;
  }
  g += `<line x1="${M.l}" y1="${y(0)}" x2="${W - M.r}" y2="${y(0)}" stroke="var(--baseline)" stroke-width="1"/>`;

  const pts = vals.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  g += `<polygon points="${x(0)},${y(0)} ${pts} ${x(vals.length - 1)},${y(0)}" fill="var(--accent)" opacity="0.10"/>`;
  g += `<polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;

  // x labels: first, weekly, last
  for (let i = 0; i < days.length; i += 7) {
    g += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle" font-size="11" fill="var(--muted)">${days[i].slice(5)}</text>`;
  }
  // end dot + end label (selective direct label)
  const li = vals.length - 1;
  g += `<circle cx="${x(li)}" cy="${y(vals[li])}" r="4.5" fill="var(--accent)" stroke="var(--surface)" stroke-width="2"/>`;

  // hover layer
  g += `<line id="xh" x1="0" y1="${M.t}" x2="0" y2="${y(0)}" stroke="var(--baseline)" stroke-width="1" visibility="hidden"/>`;
  g += `<circle id="hdot" r="4.5" fill="var(--accent)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`;
  g += `<rect x="${M.l}" y="${M.t}" width="${W - M.l - M.r}" height="${H - M.t - M.b}" fill="transparent" id="hit"/>`;
  svg.innerHTML = g;

  const tt = document.getElementById("tt");
  const hit = svg.querySelector("#hit"), xh = svg.querySelector("#xh"), hdot = svg.querySelector("#hdot");
  hit.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect();
    const mx = (ev.clientX - r.left) * (W / r.width);
    const i = Math.max(0, Math.min(days.length - 1,
      Math.round((mx - M.l) / ((W - M.l - M.r) / (days.length - 1)))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i));
    xh.setAttribute("visibility", "visible");
    hdot.setAttribute("cx", x(i)); hdot.setAttribute("cy", y(vals[i]));
    hdot.setAttribute("visibility", "visible");
    tt.style.display = "block";
    tt.querySelector(".tt-day").textContent = days[i];
    tt.querySelector(".tt-val").textContent = fmt(vals[i]) + " pages";
    const wrapR = svg.parentElement.getBoundingClientRect();
    let lx = ev.clientX - wrapR.left + 14;
    if (lx + tt.offsetWidth > wrapR.width - 4) lx = ev.clientX - wrapR.left - tt.offsetWidth - 14;
    tt.style.left = lx + "px";
    tt.style.top = (y(vals[i]) * (r.height / H) - 34) + "px";
  });
  hit.addEventListener("mouseleave", () => {
    tt.style.display = "none";
    xh.setAttribute("visibility", "hidden");
    hdot.setAttribute("visibility", "hidden");
  });

  document.getElementById("trendTable").innerHTML =
    "<table><tr><th>Day</th><th>Pages</th></tr>" +
    days.map((d, i) => `<tr><td>${d}</td><td>${fmt(vals[i])}</td></tr>`).join("") +
    "</table>";
})();

/* ---- Device rows ---- */
function meter(s) {
  if (s.pct == null) return "";
  const fill = s.pct < 10 ? "var(--critical)" : s.pct < 20 ? "var(--warning)" : "var(--accent)";
  const track = `color-mix(in oklab, ${fill} 18%, var(--surface))`;
  const short = s.desc.replace(/ Toner$/i, "").slice(0, 1);
  return `<div class="meter" title="${s.desc}: ${s.pct}%">
    <span class="mlabel">${short}</span>
    <div class="track" style="background:${track}"><div class="fill" style="width:${s.pct}%;background:${fill}"></div></div>
    <span class="mpct">${s.pct}%</span></div>`;
}
function sparkline(vals) {
  const W2 = 120, H2 = 30, n = vals.length;
  if (!n) return "";
  const mx = Math.max(1, ...vals);
  const sx = i => 2 + (W2 - 4) * (n === 1 ? 0.5 : i / (n - 1));
  const sy = v => 2 + (H2 - 4) * (1 - v / mx);
  const pts = vals.map((v, i) => `${sx(i)},${sy(v)}`).join(" ");
  const li = n - 1;
  return `<svg width="${W2}" height="${H2}" viewBox="0 0 ${W2} ${H2}" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="var(--spark)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${sx(li)}" cy="${sy(vals[li])}" r="3.5" fill="var(--accent)" stroke="var(--surface)" stroke-width="2"/></svg>`;
}
function ago(ts) {
  if (!ts) return "never";
  const h = (Date.now() - new Date(ts).getTime()) / 3.6e6;
  if (h < 1.5) return "just now";
  if (h < 36) return Math.round(h) + "h ago";
  return Math.round(h / 24) + "d ago";
}
document.getElementById("rows").innerHTML = DATA.devices.map(d => `
  <tr>
    <td class="name"><div class="nm">${d.name}</div><div class="mdl">${d.model} ${"\u00B7"} ${d.ip}</div></td>
    <td>${badge(d.status)}${d.status !== "ok" && d.detail ? `<div class="mdl" style="margin-top:2px">${d.detail}</div>` : ""}</td>
    <td><div class="meters">${d.supplies.map(meter).join("") || '<span style="color:var(--muted);font-size:12px">&mdash;</span>'}</div></td>
    <td class="pages">${fmt(d.pages)}</td>
    <td class="spark">${sparkline(d.spark)}</td>
    <td class="seen">${ago(d.last_seen)}</td>
  </tr>`).join("");
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="fleet.db")
    ap.add_argument("--out", default="fleet.html")
    ap.add_argument("--serve", type=int, metavar="PORT",
                    help="serve a live-regenerating dashboard on this port")
    args = ap.parse_args()

    if args.serve:
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                conn = fleetdb.connect(args.db)
                body = render(collect(conn)).encode()
                conn.close()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        print(f"Serving on http://127.0.0.1:{args.serve} (Ctrl+C to stop)")
        http.server.HTTPServer(("127.0.0.1", args.serve), Handler).serve_forever()
    else:
        conn = fleetdb.connect(args.db)
        html = render(collect(conn))
        conn.close()
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
