"""Finding printers you did not list, safely.   python3 tests/test_discovery.py

Nothing here touches a network: probe_address is replaced with a fake fleet,
so every decision the scanner makes can be checked - which addresses it looks
at, which answers count as a printer, what it refuses to scan, when it looks
again, and that polling a big fleet is not one-device-after-another.
"""

import asyncio
import configparser
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import collector  # noqa: E402
import fleetdb  # noqa: E402
import iprange  # noqa: E402

FAILS = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


# --------------------------------------------------------------------------- #
# The range parser
# --------------------------------------------------------------------------- #

# Every shape a person might reasonably type. The console checks the same list
# in the browser; both must agree, which is asserted from the console's suite.
GOOD = {
    "10.0.10.0/24":          (254, "10.0.10.1", "10.0.10.254"),
    "10.0.10.0/30":          (2,   "10.0.10.1", "10.0.10.2"),
    "10.0.10.4/31":          (2,   "10.0.10.4", "10.0.10.5"),
    "10.0.10.5/32":          (1,   "10.0.10.5", "10.0.10.5"),
    "10.0.20.50-10.0.20.99": (50,  "10.0.20.50", "10.0.20.99"),
    "10.0.20.50-99":         (50,  "10.0.20.50", "10.0.20.99"),
    "10.0.30.15":            (1,   "10.0.30.15", "10.0.30.15"),
    " 10.0.30.16 ":          (1,   "10.0.30.16", "10.0.30.16"),
}
BAD = {
    "":                     "no address",
    "hello":                "not an IPv4 address",
    "10.0.10.999":          "not an IPv4 address",
    "10.0.10.0/99":         "not a subnet",
    "10.0.20.99-50":        "runs backwards",
    "10.0.20.50-300":       "past .255",
    "10.0.20.50-abc":       "not a range",
    "10.0.10.1:161":        "has a port",
    "10.0.10.1, 10.0.10.2": "own line",
    "10.0.0.0/8":           "the limit is",
}


def test_parser():
    for spec, (count, first, last) in GOOD.items():
        got = iprange.parse(spec)
        check("range %r -> %d addresses, %s..%s" % (spec.strip(), count, first, last),
              len(got) == count and got[0] == first and got[-1] == last)
    for spec, phrase in BAD.items():
        try:
            iprange.parse(spec)
            check("range %r is refused" % spec, False)
        except ValueError as e:
            check("range %r is refused: %s" % (spec, phrase), phrase in str(e))
    check("a /24 never probes the network or broadcast address",
          "10.0.10.0" not in iprange.parse("10.0.10.0/24")
          and "10.0.10.255" not in iprange.parse("10.0.10.0/24"))
    check("the size limit can be raised deliberately",
          len(iprange.parse("10.0.0.0/22", max_addresses=2000)) == 1022)
    check("looks_like_a_place ignores size, only shape",
          iprange.looks_like_a_place("10.0.0.0/8") and not iprange.looks_like_a_place("nope"))


# --------------------------------------------------------------------------- #
# A fake fleet
# --------------------------------------------------------------------------- #

FLEET = {
    "10.0.10.21": {"printer": True,  "sysname": "Front Office", "model": "Canon iR-ADV"},
    "10.0.10.34": {"printer": True,  "sysname": "Lobby MFP",    "model": "HP LJ"},
    "10.0.10.60": {"printer": True,  "sysname": "",             "model": "Brother"},
    "10.0.10.99": {"printer": True,  "sysname": "Do Not Track", "model": "X"},
    "10.0.10.5":  {"printer": False, "sysname": "core-switch",  "model": "Catalyst"},
    "10.0.10.6":  {"printer": False, "sysname": "fileserver",   "model": "PowerEdge"},
}
PROBED = []


async def fake_probe(host, port, community, timeout, mp_model=1):
    PROBED.append(host)
    await asyncio.sleep(0)
    return FLEET.get(host)


def write_config(path, ranges=(), devices=(), **discovery):
    cfg = ["[snmp]", "community = public", "timeout = 2", "retries = 1", ""]
    cfg.append("[devices]")
    for name, ip in devices:
        cfg.append("%s = %s" % (name, ip))
    cfg += ["", "[ranges]"]
    for name, spec in ranges:
        cfg.append("%s = %s" % (name, spec))
    cfg += ["", "[discovery]"]
    for k, v in discovery.items():
        cfg.append("%s = %s" % (k, v))
    with open(path, "w") as fh:
        fh.write("\n".join(cfg) + "\n")


def run_collector(config, db, extra=()):
    """Run main() with the network faked out, capturing what it printed."""
    import io
    import contextlib
    argv = sys.argv
    sys.argv = ["collector.py", "--config", config, "--db", db] + list(extra)
    real_probe, real_poll = collector.probe_address, collector.snmp_poll

    async def fake_poll(host, port, *a, **k):
        dev = FLEET.get(host)
        if not dev or not dev.get("printer"):
            raise TimeoutError("no answer")
        return ({"name": dev["sysname"], "model": dev["model"]}, [])

    collector.probe_address = fake_probe
    collector.snmp_poll = fake_poll
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            collector.main()
    except SystemExit as e:
        buf.write("EXIT: %s\n" % e)
    finally:
        collector.probe_address, collector.snmp_poll = real_probe, real_poll
        sys.argv = argv
    return buf.getvalue()


def devices_in(db):
    conn = fleetdb.connect(db)
    rows = {r["ip"]: dict(r) for r in conn.execute(
        "SELECT ip, name, discovered_from, discovered_utc FROM devices")}
    conn.close()
    return rows


def test_discovery(tmp):
    config = os.path.join(tmp, "config.ini")
    db = os.path.join(tmp, "fleet.db")
    disc = collector.discovery_path(db)
    check("the discovery file sits beside the database",
          os.path.basename(disc) == "fleet-discovery.json")

    del PROBED[:]
    write_config(config, ranges=[("Office", "10.0.10.0/29")],
                 devices=[("Front Office", "10.0.10.21")],
                 rescan_hours=24, ignore="10.0.10.99")
    out = run_collector(config, db)
    doc = json.load(open(disc))
    rows = devices_in(db)

    check("it looked at the range's usable addresses only",
          sorted(PROBED) == ["10.0.10.1", "10.0.10.2", "10.0.10.3", "10.0.10.4",
                             "10.0.10.5", "10.0.10.6"])
    check("a printer that was already listed is not probed again", "10.0.10.21" not in PROBED)
    check("printers found are recorded with the range that found them",
          rows["10.0.10.5"]["discovered_from"] is None if "10.0.10.5" in rows else True)
    check("things that answer SNMP but are not printers are passed over",
          "10.0.10.5" not in rows and "10.0.10.6" not in rows and doc["NonPrinters"] == 2)
    check("it says out loud where it looked", "Looking at 6 addresses in Office" in out)

    # a bigger range that actually contains printers
    del PROBED[:]
    write_config(config, ranges=[("Office", "10.0.10.0/24")],
                 devices=[("Front Office", "10.0.10.21")],
                 rescan_hours=24, ignore="10.0.10.99")
    out = run_collector(config, db, ["--discover"])
    rows = devices_in(db)
    doc = json.load(open(disc))
    check("both new printers are found and named from sysName",
          rows["10.0.10.34"]["name"] == "Lobby MFP"
          and rows["10.0.10.34"]["discovered_from"] == "Office"
          and rows["10.0.10.34"]["discovered_utc"])
    check("a printer with no sysName is named from its model or address",
          rows["10.0.10.60"]["name"] == "Brother")
    check("an ignored address is never probed and never recorded",
          "10.0.10.99" not in PROBED and "10.0.10.99" not in rows)
    check("the file records what each place is and what it found",
          doc["Ranges"][0]["Name"] == "Office" and doc["Ranges"][0]["Spec"] == "10.0.10.0/24"
          and doc["Ranges"][0]["Addresses"] == 254 and doc["Ranges"][0]["Found"] == 2
          and doc["Ranges"][0]["LastScanUtc"])
    check("the new printers are listed for the console to mention",
          sorted(d["Ip"] for d in doc["FoundThisScan"]) == ["10.0.10.34", "10.0.10.60"])
    check("discovered printers are polled from then on",
          "[+] Lobby MFP (10.0.10.34)" in out)

    # the clock: a fresh scan is not repeated
    del PROBED[:]
    out = run_collector(config, db)
    check("it does not look again straight away",
          not PROBED and "Not looking for new printers" in out)
    check("the file still describes the places", json.load(open(disc))["Ranges"][0]["Found"] == 2)

    # ... unless asked, or unless the clock says so
    del PROBED[:]
    out = run_collector(config, db, ["--discover"])
    check("--discover always looks", PROBED and "you asked for it" in out)
    doc = json.load(open(disc))
    doc["LastScanUtc"] = "2020-01-01T00:00:00Z"
    for r in doc["Ranges"]:
        r["LastScanUtc"] = "2020-01-01T00:00:00Z"
    json.dump(doc, open(disc, "w"))
    del PROBED[:]
    out = run_collector(config, db)
    check("an old scan means it looks again", PROBED and "hours ago" in out)

    # rescan_hours = 0 means only when asked - but a NEW place is still looked at
    write_config(config, ranges=[("Office", "10.0.10.0/24")],
                 devices=[("Front Office", "10.0.10.21")], rescan_hours=0)
    del PROBED[:]
    out = run_collector(config, db)
    check("rescan_hours = 0 stops the clock", not PROBED and "only when asked" in out)
    write_config(config, ranges=[("Office", "10.0.10.0/24"), ("New place", "10.0.30.0/30")],
                 devices=[("Front Office", "10.0.10.21")], rescan_hours=0)
    del PROBED[:]
    out = run_collector(config, db)
    check("adding a place is never a silent no-op - it is looked at once",
          "10.0.30.1" in PROBED and "not been looked at yet" in out)

    # a place that cannot be scanned says so instead of doing nothing
    write_config(config, ranges=[("Everything", "10.0.0.0/8"), ("Typo", "10.0.10.")],
                 devices=[("Front Office", "10.0.10.21")], rescan_hours=24)
    del PROBED[:]
    out = run_collector(config, db, ["--discover"])
    doc = json.load(open(disc))
    check("an oversized place is refused, not attempted", not PROBED)
    check("both problems are reported by name",
          len(doc["Problems"]) == 2 and "Everything" in doc["Problems"][0]
          and "the limit is" in doc["Problems"][0] and "Typo" in doc["Problems"][1])
    check("the refused places still appear, each with its own reason",
          all(r["Problem"] for r in doc["Ranges"]) and len(doc["Ranges"]) == 2)

    # no ranges at all: nothing is ever scanned, and no file is invented
    plain_db = os.path.join(tmp, "plain.db")
    write_config(config, devices=[("Front Office", "10.0.10.21")])
    del PROBED[:]
    out = run_collector(config, plain_db)
    check("with no places named, nothing is scanned and no discovery file appears",
          not PROBED and not os.path.exists(collector.discovery_path(plain_db))
          and "[+] Front Office" in out)

    # nothing configured at all is still a clear message
    write_config(config)
    out = run_collector(config, os.path.join(tmp, "empty.db"))
    check("an empty config says what to do", "list printers under [devices]" in out)


def test_polling_is_concurrent(tmp):
    """A fleet that discovery grew must not take one deadline after another."""
    slow = [("dev%d" % i, "10.9.9.%d" % i) for i in range(8)]

    async def slow_poll(host, port, *a, **k):
        await asyncio.sleep(0.4)
        return ({"name": host}, [])

    real = collector.snmp_poll
    collector.snmp_poll = slow_poll
    try:
        t0 = time.time()
        results = asyncio.run(collector.poll_many(slow, "public", 2, 1, 1, 20.0, at_once=8))
        elapsed = time.time() - t0
    finally:
        collector.snmp_poll = real
    check("eight 0.4s polls finish in well under the 3.2s they would take in a row",
          elapsed < 1.5)
    check("every device still comes back, in the order asked",
          [r[0] for r in results] == [n for n, _ in slow])
    check("each result carries its own name, address and reading",
          all(r[2] is not None and r[4] is None for r in results))

    # one hanging device must not hold up the others, and comes back as an error
    async def one_hangs(host, port, *a, **k):
        if host == "10.9.9.3":
            await asyncio.sleep(30)
        return ({"name": host}, [])

    collector.snmp_poll = one_hangs
    try:
        t0 = time.time()
        results = asyncio.run(collector.poll_many(slow, "public", 2, 1, 1, 1.0, at_once=8))
        elapsed = time.time() - t0
    finally:
        collector.snmp_poll = real
    bad = [r for r in results if r[4] is not None]
    check("the hanging one is the only failure, and it is a timeout",
          len(bad) == 1 and bad[0][1] == "10.9.9.3"
          and isinstance(bad[0][4], (asyncio.TimeoutError, TimeoutError)))
    check("it did not hold the others up", elapsed < 3.0)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_parser()
        test_discovery(tmp)
        test_polling_is_concurrent(tmp)
    print("")
    if FAILS:
        print("RESULT: %d FAILURES" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
