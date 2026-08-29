"""Seed a synthetic printer fleet so the dashboard can be tried with zero hardware.

    python demo.py                # writes 30 days of history for 8 devices to fleet.db
    python demo.py --db x.db --days 60 --devices 12 --seed 7

The synthetic fleet is deliberately imperfect: one device has been offline for a
few days, one is jammed, one is nearly out of black toner - so the dashboard's
"needs attention" logic has something to show.
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone

import fleetdb

MODELS = [
    ("Canon imageRUNNER ADVANCE DX C5850i", ("Black", "Cyan", "Magenta", "Yellow")),
    ("Canon imageRUNNER ADVANCE DX 6860i", ("Black",)),
    ("Canon imageFORCE C7165", ("Black", "Cyan", "Magenta", "Yellow")),
    ("HP LaserJet Enterprise M611", ("Black",)),
    ("HP Color LaserJet Enterprise M776", ("Black", "Cyan", "Magenta", "Yellow")),
]

NAMES = [
    "Front Office", "Sales Bullpen", "Warehouse", "Service Bay", "Accounting",
    "Reception", "2nd Floor Copy Room", "Shipping", "Break Room", "Training Room",
    "Parts Counter", "Dispatch",
]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="fleet.db")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--devices", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = fleetdb.connect(args.db)

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=args.days)

    n = min(args.devices, len(NAMES))
    for i in range(n):
        name = NAMES[i]
        model, colors = MODELS[i % len(MODELS)]
        ip = f"10.0.10.{21 + i}"
        serial = f"DEMO{rng.randint(100000, 999999)}"
        daily_volume = rng.randint(120, 900)          # average pages/day
        pages = rng.randint(40_000, 400_000)          # lifetime count at start
        toner = {c: rng.uniform(0.35, 1.0) for c in colors}
        capacity = {c: 10_000 for c in colors}

        # Device personalities for the attention list
        offline_after = now - timedelta(days=3) if i == 2 else None   # Warehouse: offline 3 days
        jammed_today = (i == 3)                                       # Service Bay: jammed now
        toner_burner = (i == 0)                                       # Front Office: low black toner
        if toner_burner:
            toner["Black"] = 0.55  # will decline to near-empty by the end

        device_id = fleetdb.upsert_device(conn, ip, name=name, model=model,
                                          serial=serial, ts=iso(start))
        day = start
        while day <= now:
            is_today = day.date() == now.date()
            weekday = day.weekday() < 5
            pages_today = int(daily_volume * (1.0 if weekday else 0.15)
                              * rng.uniform(0.6, 1.4))
            pages += pages_today

            for c in toner:
                burn = pages_today / (capacity[c] * (1 if c == "Black" else 3.5))
                if toner_burner and c == "Black":
                    burn *= 1.6
                toner[c] -= burn
                if toner[c] <= 0.02 and not (toner_burner and c == "Black"):
                    toner[c] = 1.0  # cartridge replaced

            unreachable = offline_after is not None and day >= offline_after
            if unreachable:
                status, detail, reachable = "offline", "No SNMP response", False
                supplies = []
            else:
                if jammed_today and is_today:
                    status, detail = "error", "Paper jam"
                elif any(v < 0.10 for v in toner.values()):
                    low = [c for c, v in toner.items() if v < 0.10]
                    status, detail = "warning", f"Low toner: {', '.join(low)}"
                else:
                    status, detail = "ok", "Idle"
                reachable = True
                supplies = [
                    {"slot": s + 1, "description": f"{c} Toner",
                     "supply_type": "toner",
                     "level": max(0, int(toner[c] * capacity[c])),
                     "max_capacity": capacity[c]}
                    for s, c in enumerate(toner)
                ]

            fleetdb.insert_snapshot(
                conn, device_id, iso(day), reachable, status, detail,
                uptime_seconds=rng.randint(86_400, 90 * 86_400),
                page_count=pages, supplies=supplies,
            )
            if not unreachable:
                conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?",
                             (iso(day), device_id))
            day += timedelta(days=1)

    conn.commit()
    ndev = conn.execute("SELECT COUNT(*) c FROM devices").fetchone()["c"]
    nsnap = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
    print(f"Seeded {ndev} devices / {nsnap} snapshots into {args.db}")
    print("Next: python dashboard.py --db", args.db)


if __name__ == "__main__":
    main()
