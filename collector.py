"""Poll printer fleet over SNMP (Printer MIB / RFC 3805) into the SQLite database.

    python collector.py --config config.ini --db fleet.db

Run it on a schedule (cron / Task Scheduler); each run appends one snapshot per
device. Unreachable devices are recorded too, so the dashboard can show them
as offline. Requires: pip install "pysnmp>=7.1"

A printer that does not answer and a missing SNMP library are DIFFERENT facts
and never produce the same words. If the library is not there, nothing is
polled, nothing is written, no device is marked offline, and the exit code is
non-zero so whatever ran this knows the fleet was not checked at all.

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

    [ranges]
    ; Places to LOOK for printers you have not listed. Each one is a subnet,
    ; a span, or a single address; anything that answers the Printer MIB is
    ; added and polled from then on. Leave this out and nothing is scanned.
    Office = 10.0.10.0/24

    [discovery]
    rescan_hours = 24         ; 0 = only when you pass --discover
    ; ignore = 10.0.10.99

A word about scanning: with [ranges] set, this sends an SNMP GET to every
address in them. That is ordinary traffic on a network you run, but it is
still a scan - on some networks it will show up in monitoring. Nothing is
scanned unless you name a place, and a place bigger than 1024 addresses is
refused rather than attempted.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import fleetdb
import iprange

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

# Exit code for "the SNMP library is not usable here". Distinct from 1 so a
# caller can tell "the fleet was not checked" from any other failure.
SNMP_MISSING_EXIT = 3


def import_snmp():
    """THE one place pysnmp is imported, so the pre-flight and the real work
    ask exactly the same question.

    This suite has already been bitten once by a pre-flight that asked a
    DIFFERENT question than the operation it was guarding: a certificate check
    that took a key handle, said "usable", and then watched the real sign-in
    fail. A check that can disagree with the work is worse than no check,
    because it reports fine on the machine where the thing then breaks. So
    require_snmp() calls this, snmp_poll() calls this, probe_address() calls
    this, and there is nothing left for them to disagree about.
    """
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
        ObjectType, ObjectIdentity, get_cmd, walk_cmd,
    )
    return SimpleNamespace(
        SnmpEngine=SnmpEngine, CommunityData=CommunityData,
        UdpTransportTarget=UdpTransportTarget, ContextData=ContextData,
        ObjectType=ObjectType, ObjectIdentity=ObjectIdentity,
        get_cmd=get_cmd, walk_cmd=walk_cmd)


def snmp_missing_words(error):
    """What to tell a person who is not going to know what pysnmp is."""
    return "\n".join([
        "",
        "  The printer checker needs a piece of Python called pysnmp, and the",
        "  account that ran this cannot see it (%s)." % error,
        "",
        "  NOTHING WAS CHANGED. No printer was contacted, nothing was written",
        "  down, and no printer has been marked offline - the printer page still",
        "  shows the last real reading, marked as not updated by this run.",
        "",
        "  Install it for the WHOLE COMPUTER, not just for one person. The daily",
        "  refresh runs as the computer itself, which cannot see a library that",
        "  was installed for a single account - that is the usual cause of this.",
        "  In a PowerShell window opened with 'Run as administrator':",
        "",
        '      & "%s" -m pip install "pysnmp>=7.1"' % sys.executable,
        "",
    ])


def require_snmp():
    """Ask the library question ONCE, up front, before anything is polled or
    written. Returns True, or prints plain words and returns False."""
    try:
        import_snmp()
    except ImportError as e:
        print("[x] The printers could not be checked.")
        print(snmp_missing_words(e))
        return False
    return True


async def snmp_poll(host: str, port: int, community: str, timeout: float, retries: int,
                    mp_model: int = 1):
    """Return (fields dict, supplies list). Raises on unreachable/timeout."""
    snmp = import_snmp()
    ObjectType, ObjectIdentity = snmp.ObjectType, snmp.ObjectIdentity

    engine = snmp.SnmpEngine()
    auth = snmp.CommunityData(community, mpModel=mp_model)  # 0 = SNMPv1, 1 = v2c
    transport = await snmp.UdpTransportTarget.create((host, port),
                                                     timeout=timeout, retries=retries)
    try:
        # -- scalars (one GET; tolerate per-OID noSuchObject) --
        oids = [OID_UPTIME, OID_SYSNAME, OID_MODEL, OID_SERIAL,
                OID_PRT_STATUS, OID_ERR_STATE, OID_LIFE_COUNT]
        err_ind, err_stat, _, var_binds = await snmp.get_cmd(
            engine, auth, transport, snmp.ContextData(),
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
            objects = snmp.walk_cmd(engine, auth, transport, snmp.ContextData(),
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


async def poll_one(display_name, address, community, timeout, retries, mp_model=1,
                   deadline=20.0):
    """One device's whole exchange, with a hard wall-clock ceiling.

    The per-request timeout/retries bound each SNMP call, but a flaky device
    that answers some calls and stalls on others (or wedges a supply walk)
    could still drag the poll out or hang it. This guarantees we move on, so
    one unresponsive printer can never freeze the whole refresh.
    """
    host, _, port = address.partition(":")
    return await asyncio.wait_for(
        snmp_poll(host.strip(), int(port) if port else 161,
                  community, timeout, retries, mp_model),
        timeout=deadline)


def poll_device(display_name, address, community, timeout, retries, mp_model=1,
                deadline=20.0):
    """Poll one device on its own (used by tests and anything single-shot)."""
    fields, supplies = asyncio.run(
        poll_one(display_name, address, community, timeout, retries, mp_model, deadline))
    return normalize(fields, supplies), supplies


async def poll_many(devices, community, timeout, retries, mp_model, deadline, at_once=8):
    """Poll every device, a few at a time, and hand back results IN ORDER.

    Polling used to be strictly one device after another, which was fine for a
    hand-typed list of five. Discovery can turn that into fifty, and a handful
    of offline ones would then add their whole deadline each, one after the
    other - minutes of a refresh spent waiting. A small pool keeps a big fleet
    quick without flooding the network or the printers.
    """
    gate = asyncio.Semaphore(max(1, int(at_once)))

    async def one(name, address):
        async with gate:
            try:
                fields, supplies = await poll_one(name, address, community, timeout,
                                                  retries, mp_model, deadline)
                return name, address, normalize(fields, supplies), supplies, None
            except Exception as e:  # noqa: BLE001 - every failure is "offline"
                return name, address, None, None, e

    return await asyncio.gather(*(one(n, a) for n, a in devices))


# --------------------------------------------------------------------------- #
# Discovery: look for printers in the places config.ini names
# --------------------------------------------------------------------------- #

# A probe is deliberately cheap and quiet: one GET of four scalars. An address
# only counts as a printer if it answers something from the Printer MIB - a
# switch or a server that happens to speak SNMP is passed over, not recorded.
async def probe_address(host, port, community, timeout, mp_model=1):
    """Return None (nothing answered) or {'printer': bool, 'sysname', 'model'}."""
    snmp = import_snmp()
    ObjectType, ObjectIdentity = snmp.ObjectType, snmp.ObjectIdentity
    engine = snmp.SnmpEngine()
    auth = snmp.CommunityData(community, mpModel=mp_model)
    transport = await snmp.UdpTransportTarget.create((host, port), timeout=timeout,
                                                     retries=0)
    try:
        err_ind, err_stat, _idx, binds = await snmp.get_cmd(
            engine, auth, transport, snmp.ContextData(),
            ObjectType(ObjectIdentity(OID_SYSNAME)),
            ObjectType(ObjectIdentity(OID_MODEL)),
            ObjectType(ObjectIdentity(OID_PRT_STATUS)),
            ObjectType(ObjectIdentity(OID_LIFE_COUNT)))
        if err_ind or err_stat:
            return None
        values = {}
        for oid, val in binds:
            text = val.prettyPrint()
            if text and "No Such" not in text:
                values[str(oid)] = text
        if not values:
            return None
        printer = any(str(o).startswith(p.rstrip("."))
                      for o in values for p in (OID_PRT_STATUS, OID_LIFE_COUNT))
        return {"printer": printer,
                "sysname": values.get(OID_SYSNAME) or "",
                "model": values.get(OID_MODEL) or ""}
    finally:
        try:
            engine.close_dispatcher()
        except Exception:  # noqa: BLE001 - best effort
            pass


async def scan_addresses(addresses, community, timeout, mp_model, at_once=64):
    """Probe every address, a bounded number at a time. -> {address: result}"""
    gate = asyncio.Semaphore(max(1, int(at_once)))
    out = {}

    async def one(addr):
        async with gate:
            try:
                out[addr] = await probe_address(addr, 161, community, timeout, mp_model)
            except Exception:  # noqa: BLE001 - an address that errors is just "nothing there"
                out[addr] = None

    await asyncio.gather(*(one(a) for a in addresses))
    return out


def known_addresses(conn):
    return {row["ip"] for row in conn.execute("SELECT ip FROM devices")}


def run_discovery(conn, places, community, timeout, mp_model, ignore, max_addresses,
                  at_once, ts):
    """Look through every configured place. Returns (per-range info, problems,
    newly found devices, how many answered but were not printers)."""
    known = known_addresses(conn)
    per_range, problems, found_new, non_printers = [], [], [], 0

    for name, spec in places:
        entry = {"Name": name, "Spec": spec, "Addresses": 0, "LastScanUtc": ts,
                 "Found": 0, "Problem": None}
        try:
            addresses = iprange.parse(spec, max_addresses)
        except ValueError as e:
            entry["Problem"] = str(e)
            problems.append("%s: %s" % (name, e))
            per_range.append(entry)
            print("[!] %s (%s): %s" % (name, spec, e))
            continue
        entry["Addresses"] = len(addresses)
        todo = [a for a in addresses if a not in known and a not in ignore]
        print("[*] Looking at %d address%s in %s (%s)%s ..."
              % (len(addresses), "" if len(addresses) == 1 else "es", name, spec,
                 "" if len(todo) == len(addresses)
                 else " - %d already known or ignored" % (len(addresses) - len(todo))))
        results = asyncio.run(scan_addresses(todo, community, timeout, mp_model, at_once))
        for addr in todo:
            res = results.get(addr)
            if not res:
                continue
            if not res.get("printer"):
                non_printers += 1
                continue
            label = (res.get("sysname") or res.get("model") or addr).strip() or addr
            fleetdb.upsert_device(conn, addr, name=label, model=res.get("model") or None,
                                  ts=ts, discovered_from=name)
            known.add(addr)
            found_new.append({"Ip": addr, "Name": label, "Range": name})
            entry["Found"] += 1
            print("[+] found a printer at %s (%s) in %s" % (addr, label, name))
        per_range.append(entry)
    conn.commit()
    return per_range, problems, found_new, non_printers


def discovery_path(db_path):
    """fleet.db -> fleet-discovery.json, beside it."""
    base, _ext = os.path.splitext(db_path)
    return base + "-discovery.json"


def read_discovery(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_discovery(path, doc):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
    except OSError as e:
        print("[!] could not write %s (%s)" % (path, e))


def scan_is_due(places, previous, rescan_hours, ts):
    """A place that has NEVER been looked at is always looked at once - adding a
    range and having nothing happen is exactly the silent no-op this feature is
    meant to avoid. After that, the clock decides (0 = only when asked)."""
    seen = {r.get("Spec"): r.get("LastScanUtc") for r in (previous.get("Ranges") or [])}
    for _name, spec in places:
        if not seen.get(spec):
            return True, "a place has not been looked at yet"
    if not rescan_hours:
        return False, "rescan_hours is 0 - looking only when asked"
    last = previous.get("LastScanUtc")
    if not last:
        return True, "nothing has been scanned yet"
    try:
        age = (datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
               - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 3600.0
    except ValueError:
        return True, "the last scan time could not be read"
    if age >= rescan_hours:
        return True, "the last look was %.0f hours ago" % age
    return False, "looked %.0f hours ago; looking again after %g" % (age, rescan_hours)


def split_list(text):
    return [p.strip() for p in str(text or "").replace(",", ";").split(";") if p.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--db", default="fleet.db")
    ap.add_argument("--discover", action="store_true",
                    help="look through the places in [ranges] now, whatever the clock says")
    ap.add_argument("--no-discover", action="store_true",
                    help="poll what is already known and look for nothing new")
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
    # Hard per-device ceiling so one unresponsive printer can never wedge the
    # whole poll. Defaults to comfortably longer than a healthy poll, scaled off
    # the per-request timeout; override with "device_timeout = <seconds>".
    device_timeout = cfg.getfloat("snmp", "device_timeout",
                                  fallback=max(10.0, timeout * (retries + 1) * 4 + 4))
    at_once = cfg.getint("snmp", "poll_at_once", fallback=8)

    devices = dict(cfg.items("devices")) if cfg.has_section("devices") else {}
    places = list(cfg.items("ranges")) if cfg.has_section("ranges") else []
    rescan_hours = cfg.getfloat("discovery", "rescan_hours", fallback=24.0)
    ignore = set(split_list(cfg.get("discovery", "ignore", fallback="")))
    probe_timeout = cfg.getfloat("discovery", "timeout", fallback=1.0)
    scan_at_once = cfg.getint("discovery", "scan_at_once", fallback=64)
    max_addresses = cfg.getint("discovery", "max_addresses",
                               fallback=iprange.DEFAULT_MAX_ADDRESSES)
    if not devices and not places:
        sys.exit("Nothing to do: list printers under [devices], or places to look under [ranges].")

    # Ask the library question BEFORE the database is opened, before anything is
    # polled, and before a single row is written. A missing library means we
    # learned nothing about any printer - and a run that learned nothing must
    # not leave evidence behind that looks like something it learned.
    if not require_snmp():
        sys.exit(SNMP_MISSING_EXIT)

    conn = fleetdb.connect(args.db)
    ts = fleetdb.utcnow_iso()
    disc_path = discovery_path(args.db)
    previous = read_discovery(disc_path)

    # ---- look for new printers ------------------------------------------- #
    per_range, problems, found_new, non_printers = [], [], [], 0
    scanned = False
    if places and not args.no_discover:
        due, why = scan_is_due(places, previous, rescan_hours, ts)
        if args.discover:
            due, why = True, "you asked for it"
        if due:
            print(f"Looking for printers ({why}).")
            try:
                per_range, problems, found_new, non_printers = run_discovery(
                    conn, places, community, probe_timeout, mp_model, ignore,
                    max_addresses, scan_at_once, ts)
            except ImportError as e:
                # Not "we looked and found nothing" - we never looked. Saying
                # "Found 0 new printer(s)" here would be the same lie the poll
                # used to tell, in the one place a person is least able to
                # catch it: an empty range looks exactly like a quiet one.
                print("[x] The search for new printers could not run.")
                print(snmp_missing_words(e))
                sys.exit(SNMP_MISSING_EXIT)
            scanned = True
            print(f"Found {len(found_new)} new printer(s).")
        else:
            print(f"Not looking for new printers ({why}).")
            per_range = previous.get("Ranges") or []
            problems = previous.get("Problems") or []

    # Ranges the previous file knew nothing about still deserve a row, so the
    # console can show "not looked at yet" rather than nothing at all.
    if not scanned:
        by_spec = {r.get("Spec"): r for r in per_range}
        per_range = []
        for name, spec in places:
            row = dict(by_spec.get(spec) or {})
            row.setdefault("Name", name)
            row.setdefault("Spec", spec)
            row.setdefault("Addresses", 0)
            row.setdefault("Found", 0)
            row.setdefault("LastScanUtc", None)
            row.setdefault("Problem", None)
            row["Name"] = name
            per_range.append(row)

    # ---- poll everything we know about ------------------------------------ #
    for row in conn.execute("SELECT ip, name FROM devices WHERE discovered_from IS NOT NULL"):
        if row["ip"] in ignore:
            continue
        if row["ip"] not in devices.values():
            devices.setdefault(row["name"] or row["ip"], row["ip"])
    poll_list = [(n, a.strip()) for n, a in devices.items() if a.strip() not in ignore]

    ok = fail = broke = 0
    if poll_list:
        results = asyncio.run(poll_many(poll_list, community, timeout, retries, mp_model,
                                        device_timeout, at_once))
        for name, ip, normalized, supplies, error in results:
            if error is None:
                status, detail, uptime_s, pages, model, serial, sysname = normalized
                device_id = fleetdb.upsert_device(conn, ip, name=name or sysname,
                                                  model=model, serial=serial, ts=ts)
                fleetdb.insert_snapshot(conn, device_id, ts, True, status, detail,
                                        uptime_s, pages, supplies)
                print(f"[+] {name} ({ip}): {status} - {detail}")
                ok += 1
            elif isinstance(error, ImportError):
                # The SNMP library went missing part-way (require_snmp passed,
                # so this means one of pysnmp's own optional imports failed).
                # Record NOTHING. An invented "offline" row outlives the run:
                # it lands in the history, drags the dashboard's online count
                # down, and puts "check the printer is powered on" in front of
                # somebody about a printer that was never asked anything.
                print(f"[x] {name} ({ip}): could not be checked - {error}")
                broke += 1
            else:
                row = conn.execute("SELECT id FROM devices WHERE ip = ?", (ip,)).fetchone()
                if row:
                    device_id = row["id"]
                else:
                    cur = conn.execute(
                        "INSERT INTO devices (ip, name, first_seen, last_seen)"
                        " VALUES (?, ?, ?, ?)", (ip, name, ts, ts))
                    device_id = cur.lastrowid
                detail = (f"No SNMP response (timed out after {device_timeout:g}s)"
                          if isinstance(error, (asyncio.TimeoutError, TimeoutError))
                          else f"No SNMP response ({type(error).__name__})")
                fleetdb.insert_snapshot(conn, device_id, ts, False, "offline",
                                        detail, None, None)
                print(f"[!] {name} ({ip}): unreachable ({error or type(error).__name__})")
                fail += 1
            conn.commit()  # keep what we have even if a later device blows up

    conn.commit()

    # ---- say where we looked, for the console ----------------------------- #
    if places or previous:
        write_discovery(disc_path, {
            "GeneratedUtc": ts,
            "Ranges": per_range,
            "Ignored": sorted(ignore),
            "RescanHours": rescan_hours,
            "MaxAddresses": max_addresses,
            "LastScanUtc": ts if scanned else previous.get("LastScanUtc"),
            "ScannedThisRun": scanned,
            "FoundThisScan": found_new if scanned else [],
            "NonPrinters": non_printers if scanned else previous.get("NonPrinters", 0),
            "Problems": problems,
        })

    if broke:
        print(f"[x] Stopped: {broke} printer(s) could not be checked because the SNMP")
        print("    library failed while the run was going. Nothing was recorded for")
        print("    them - they have NOT been marked offline.")
        sys.exit(SNMP_MISSING_EXIT)

    print(f"Done: {ok} polled, {fail} unreachable. Next: python dashboard.py --db {args.db}")


if __name__ == "__main__":
    main()
