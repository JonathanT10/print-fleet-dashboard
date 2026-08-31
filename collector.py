"""Poll printer fleet over SNMP (Printer MIB / RFC 3805) into the SQLite database.

    python collector.py --config config.ini --db fleet.db

Run it on a schedule (cron / Task Scheduler); each run appends one snapshot per
device. Unreachable devices are recorded too, so the dashboard can show them
as offline. Requires: pip install "pysnmp>=7.1"

Config (INI):

    [snmp]
    community = public
    timeout = 2
    retries = 1
    ; version = 1        <- Canon iR-ADV fleets: they answer SNMPv1 only

    [devices]
    Front Office = 10.0.10.21
    Warehouse    = 10.0.10.22
    ; a nonstandard port is fine: Lab = 10.0.10.30:1161
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import sys

import fleetdb

# --- Printer MIB / HOST-RESOURCES OIDs (scalar-per-printer instances) --------
OID_UPTIME      = "1.3.6.1.2.1.1.3.0"
OID_SYSNAME     = "1.3.6.1.2.1.1.5.0"
OID_MODEL       = "1.3.6.1.2.1.25.3.2.1.3.1"        # hrDeviceDescr.1
OID_SERIAL      = "1.3.6.1.2.1.43.5.1.1.17.1"       # prtGeneralSerialNumber
OID_PRT_STATUS  = "1.3.6.1.2.1.25.3.5.1.1.1"        # hrPrinterStatus
OID_ERR_STATE   = "1.3.6.1.2.1.25.3.5.1.2.1"        # hrPrinterDetectedErrorState
OID_LIFE_COUNT  = "1.3.6.1.2.1.43.10.2.1.4.1.1"     # prtMarkerLifeCount
OID_SUP_TYPE    = "1.3.6.1.2.1.43.11.1.1.5"         # prtMarkerSuppliesType
OID_SUP_DESC    = "1.3.6.1.2.1.43.11.1.1.6"         # prtMarkerSuppliesDescription
OID_SUP_MAX     = "1.3.6.1.2.1.43.11.1.1.8"         # prtMarkerSuppliesMaxCapacity
OID_SUP_LEVEL   = "1.3.6.1.2.1.43.11.1.1.9"         # prtMarkerSuppliesLevel

PRINTER_STATUS = {1: "other", 2: "unknown", 3: "idle", 4: "printing", 5: "warmup"}

# hrPrinterDetectedErrorState, first byte (RFC 2790)
ERROR_BITS = [
    (0x80, "Low paper",         "warning"),
    (0x40, "Out of paper",      "error"),
    (0x20, "Low toner",         "warning"),
    (0x10, "Out of toner",      "error"),
    (0x08, "Door open",         "error"),
    (0x04, "Paper jam",         "error"),
    (0x02, "Offline",           "error"),
    (0x01, "Service requested", "error"),
]

SUPPLY_TYPES = {3: "toner", 4: "waste-toner", 5: "ink", 6: "ink-cartridge",
                8: "developer", 9: "drum", 10: "coronaWire", 12: "fuser",
                15: "fuser-oil", 21: "staples"}


# --------------------------------------------------------------------------- #
# SNMP (pysnmp >= 7, asyncio hlapi)
# --------------------------------------------------------------------------- #

async def snmp_poll(host: str, port: int, community: str, timeout: float, retries: int,
                    mp_model: int = 1):
    """Return (fields dict, supplies list). Raises on unreachable/timeout."""
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
        ObjectType, ObjectIdentity, get_cmd, walk_cmd,
    )

    engine = SnmpEngine()
    auth = CommunityData(community, mpModel=mp_model)  # 0 = SNMPv1, 1 = SNMPv2c
    transport = await UdpTransportTarget.create((host, port),
                                                timeout=timeout, retries=retries)
    try:
        # -- scalars (one GET; tolerate per-OID noSuchObject) --
        oids = [OID_UPTIME, OID_SYSNAME, OID_MODEL, OID_SERIAL,
                OID_PRT_STATUS, OID_ERR_STATE, OID_LIFE_COUNT]
        err_ind, err_stat, _, var_binds = await get_cmd(
            engine, auth, transport, ContextData(),
            *[ObjectType(ObjectIdentity(o)) for o in oids],
        )
        if err_ind:
            raise ConnectionError(str(err_ind))
        if err_stat:
            raise ConnectionError(err_stat.prettyPrint())

        fields = {}
        for oid, val in var_binds:
            key = str(oid)
            if val is None or val.__class__.__name__ in (
                    "NoSuchObject", "NoSuchInstance", "EndOfMibView"):
                continue
            fields[key] = val

        # -- supplies (walk four columns) --
        async def walk(base):
            out = {}
            objects = walk_cmd(engine, auth, transport, ContextData(),
                               ObjectType(ObjectIdentity(base)))
            async for w_err_ind, w_err_stat, _, w_binds in objects:
                if w_err_ind or w_err_stat:
                    break
                for oid, val in w_binds:
                    s = str(oid)
                    if not s.startswith(base + "."):
                        return out
                    out[s[len(base) + 1:]] = val   # key: "hrDeviceIndex.slot"
            return out

        desc = await walk(OID_SUP_DESC)
        typ = await walk(OID_SUP_TYPE)
        mx = await walk(OID_SUP_MAX)
        lvl = await walk(OID_SUP_LEVEL)

        supplies = []
        for idx in sorted(desc, key=lambda k: [int(p) for p in k.split(".")]):
            slot = int(idx.split(".")[-1])
            t = typ.get(idx)
            supplies.append({
                "slot": slot,
                "description": bytes(desc[idx]).decode("latin-1", "replace").strip("\x00 "),
                "supply_type": SUPPLY_TYPES.get(int(t) if t is not None else -1, "other"),
                "level": int(lvl[idx]) if idx in lvl else None,
                "max_capacity": int(mx[idx]) if idx in mx else None,
            })
        return fields, supplies
    finally:
        engine.close_dispatcher()


def normalize(fields, supplies):
    """Map raw SNMP values to (status, detail, uptime, pages, model, serial, name)."""
    def get(oid):
        return fields.get(oid)

    uptime = get(OID_UPTIME)
    uptime_s = int(uptime) // 100 if uptime is not None else None
    name = get(OID_SYSNAME)
    name = bytes(name).decode("latin-1", "replace").strip("\x00 ") if name is not None else None
    model = get(OID_MODEL)
    model = bytes(model).decode("latin-1", "replace").strip("\x00 ") if model is not None else None
    serial = get(OID_SERIAL)
    serial = bytes(serial).decode("latin-1", "replace").strip("\x00 ") if serial is not None else None
    pages = get(OID_LIFE_COUNT)
    pages = int(pages) if pages is not None else None

    problems, worst = [], "ok"
    err = get(OID_ERR_STATE)
    if err is not None:
        raw = bytes(err)
        first = raw[0] if raw else 0
        for bit, label, sev in ERROR_BITS:
            if first & bit:
                problems.append(label)
                if sev == "error":
                    worst = "error"
                elif worst != "error":
                    worst = "warning"

    if worst == "ok":
        low = [s["description"] for s in supplies
               if s["max_capacity"] and s["level"] is not None and s["level"] >= 0
               and s["level"] / s["max_capacity"] < 0.10]
        if low:
            worst, problems = "warning", [f"Low: {', '.join(low)}"]

    prt = get(OID_PRT_STATUS)
    state_word = PRINTER_STATUS.get(int(prt), "unknown") if prt is not None else "unknown"
    detail = "; ".join(problems) if problems else state_word.capitalize()
    return worst, detail, uptime_s, pages, model, serial, name


def poll_device(display_name, address, community, timeout, retries, mp_model=1):
    host, _, port = address.partition(":")
    fields, supplies = asyncio.run(
        snmp_poll(host.strip(), int(port) if port else 161,
                  community, timeout, retries, mp_model))
    return normalize(fields, supplies), supplies


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--db", default="fleet.db")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    cfg.optionxform = str  # keep device-name capitalization
    if not cfg.read(args.config):
        sys.exit(f"Config not found: {args.config} (copy config.example.ini)")
    community = cfg.get("snmp", "community", fallback="public")
    timeout = cfg.getfloat("snmp", "timeout", fallback=2.0)
    retries = cfg.getint("snmp", "retries", fallback=1)
    # Canon iR-ADV devices commonly answer SNMPv1 ONLY and silently ignore
    # v2c - set "version = 1" in [snmp] for those fleets. Default: 2c.
    version = cfg.get("snmp", "version", fallback="2c").strip().lower()
    mp_model = 0 if version in ("1", "v1") else 1
    devices = dict(cfg.items("devices")) if cfg.has_section("devices") else {}
    if not devices:
        sys.exit("No [devices] configured.")

    conn = fleetdb.connect(args.db)
    ts = fleetdb.utcnow_iso()
    ok = fail = 0

    for name, address in devices.items():
        ip = address.strip()
        try:
            (status, detail, uptime_s, pages, model, serial, sysname), supplies = \
                poll_device(name, ip, community, timeout, retries, mp_model)
            device_id = fleetdb.upsert_device(conn, ip, name=name or sysname,
                                              model=model, serial=serial, ts=ts)
            fleetdb.insert_snapshot(conn, device_id, ts, True, status, detail,
                                    uptime_s, pages, supplies)
            print(f"[+] {name} ({ip}): {status} - {detail}")
            ok += 1
        except Exception as e:  # noqa: BLE001 - record any failure as offline
            row = conn.execute("SELECT id FROM devices WHERE ip = ?", (ip,)).fetchone()
            if row:
                device_id = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO devices (ip, name, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?)", (ip, name, ts, ts))
                device_id = cur.lastrowid
            fleetdb.insert_snapshot(conn, device_id, ts, False, "offline",
                                    f"No SNMP response ({type(e).__name__})",
                                    None, None)
            print(f"[!] {name} ({ip}): unreachable ({e})")
            fail += 1
        conn.commit()  # keep what we have even if a later device blows up

    conn.commit()
    print(f"Done: {ok} polled, {fail} unreachable. Next: python dashboard.py --db {args.db}")


if __name__ == "__main__":
    main()
