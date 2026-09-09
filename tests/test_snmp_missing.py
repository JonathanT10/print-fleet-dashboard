"""A missing SNMP library is not an offline printer.   python3 tests/test_snmp_missing.py

The bug this guards against, found in a real 07:00 scheduled run: pysnmp was
installed for one person's account and the scheduled job runs as the computer
itself, so the import failed. The failure was raised per device, caught by the
"every failure is offline" handler, and written into the database as three real
snapshots reading "No SNMP response (ModuleNotFoundError)". The collector then
exited 0, the refresh called the step "ok", and the console told somebody to go
check that three healthy printers were powered on.

Three things have to be true for that to be impossible:
  1. the library question is asked ONCE, up front, and stops the run;
  2. nothing is written when it fails - no snapshot, no invented offline row;
  3. the exit code is non-zero, so the caller knows the fleet was not checked.

And one thing must NOT change: a printer that genuinely does not answer is
still recorded as offline, exactly as before.
"""

import contextlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import collector  # noqa: E402
import fleetdb  # noqa: E402

FAILS = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def write_config(path, devices):
    lines = ["[snmp]", "community = public", "timeout = 0.1", "retries = 0", "",
             "[devices]"]
    for name, addr in devices.items():
        lines.append("%s = %s" % (name, addr))
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def run_collector(config, db, import_error=None, poll=None, extra=()):
    """Run main(), capturing stdout and the exit code.

    import_error: an exception instance to raise from import_snmp() - the one
    place pysnmp is imported, so faking it here is exactly what a machine
    without the library does.
    """
    argv = sys.argv
    sys.argv = ["collector.py", "--config", config, "--db", db] + list(extra)
    real_import, real_poll = collector.import_snmp, collector.snmp_poll

    def fake_import():
        if import_error is not None:
            raise import_error
        return real_import()

    async def default_poll(host, port, *a, **k):
        raise TimeoutError("no answer")

    collector.import_snmp = fake_import
    collector.snmp_poll = poll or default_poll
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            collector.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        collector.import_snmp, collector.snmp_poll = real_import, real_poll
        sys.argv = argv
    return buf.getvalue(), code


def snapshots(db):
    if not os.path.exists(db):
        return []
    conn = fleetdb.connect(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT d.ip, s.reachable, s.status, s.detail"
            " FROM snapshots s JOIN devices d ON d.id = s.device_id")]
    finally:
        conn.close()


THREE = {"Front Stairs": "10.9.9.1", "Jim's Office": "10.9.9.2", "C5540": "10.9.9.3"}


# --------------------------------------------------------------------------- #
# 1. The pre-flight itself
# --------------------------------------------------------------------------- #

def test_require_snmp():
    real = collector.import_snmp

    def boom():
        raise ModuleNotFoundError("No module named 'pysnmp'")

    collector.import_snmp = boom
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ok = collector.require_snmp()
    finally:
        collector.import_snmp = real
    words = buf.getvalue()

    check("require_snmp() says no when the library is missing", ok is False)
    check("it names pysnmp so the fix is findable", "pysnmp" in words)
    check("it says nothing was changed",
          "NOTHING WAS CHANGED" in words)
    check("it does NOT use the word unreachable",
          "unreachable" not in words.lower())
    check("it reassures that no printer was marked offline",
          "no printer has been marked offline" in words.lower())
    check("it does NOT tell anyone to go look at a printer",
          "powered on" not in words.lower())
    check("it explains the whole-computer vs one-account trap",
          "WHOLE COMPUTER" in words and "single account" in words)
    check("it gives a command with the interpreter that actually ran it",
          sys.executable in words and "pip install" in words)

    collector.import_snmp = real
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = collector.require_snmp()
    check("require_snmp() says yes when the library is there", ok is True)


# --------------------------------------------------------------------------- #
# 2. A missing library records nothing at all
# --------------------------------------------------------------------------- #

def test_missing_library_writes_nothing(tmp):
    cfg = os.path.join(tmp, "missing.ini")
    db = os.path.join(tmp, "missing.db")
    write_config(cfg, THREE)
    out, code = run_collector(cfg, db,
                              import_error=ModuleNotFoundError("No module named 'pysnmp'"))

    check("it exits non-zero", code != 0)
    check("it exits with the library code, not a generic 1",
          code == collector.SNMP_MISSING_EXIT)
    check("NOT ONE snapshot row was written", snapshots(db) == [])
    check("no printer was called unreachable", "unreachable" not in out.lower())
    check("it does not claim a count of printers polled",
          "Done:" not in out and "polled" not in out)
    check("it says the printers could not be checked",
          "could not be checked" in out.lower()
          or "printers could not be checked" in out.lower())


# --------------------------------------------------------------------------- #
# 3. The behaviour that must NOT change: a real offline printer
# --------------------------------------------------------------------------- #

def test_a_genuinely_dead_printer_is_still_offline(tmp):
    cfg = os.path.join(tmp, "dead.ini")
    db = os.path.join(tmp, "dead.db")
    write_config(cfg, THREE)
    out, code = run_collector(cfg, db)   # default poll raises TimeoutError

    rows = snapshots(db)
    check("a printer that does not answer is still recorded", len(rows) == 3)
    check("...as offline", rows and all(r["status"] == "offline" for r in rows))
    check("...and unreachable", rows and all(r["reachable"] == 0 for r in rows))
    check("that run still exits 0 - offline printers are not a failure", code == 0)
    check("and it still reports the count", "3 unreachable" in out)


# --------------------------------------------------------------------------- #
# 4. A library failure part-way through is still not an offline printer
# --------------------------------------------------------------------------- #

def test_import_error_during_the_poll(tmp):
    cfg = os.path.join(tmp, "midrun.ini")
    db = os.path.join(tmp, "midrun.db")
    write_config(cfg, THREE)

    async def poll_raises_import(host, port, *a, **k):
        raise ImportError("cannot import name 'AesCfb128Decrypter'")

    out, code = run_collector(cfg, db, poll=poll_raises_import)

    check("a mid-run library failure exits with the library code",
          code == collector.SNMP_MISSING_EXIT)
    check("no invented offline row was written for it", snapshots(db) == [])
    check("it is not described as unreachable", "unreachable" not in out.lower())
    check("it says those printers could not be checked",
          "could not be checked" in out.lower())
    check("it says they were not marked offline",
          "not been marked offline" in out.lower())


# --------------------------------------------------------------------------- #
# 5. Discovery does not report "found nothing" when it never looked
# --------------------------------------------------------------------------- #

def test_discovery_never_says_found_zero(tmp):
    cfg = os.path.join(tmp, "ranges.ini")
    db = os.path.join(tmp, "ranges.db")
    with open(cfg, "w") as fh:
        fh.write("[snmp]\ncommunity = public\ntimeout = 0.1\nretries = 0\n\n"
                 "[devices]\nFront Stairs = 10.9.9.1\n\n"
                 "[ranges]\nOffice = 10.9.9.0/29\n\n"
                 "[discovery]\nrescan_hours = 24\n")

    # The library is gone before anything runs: the up-front check must stop
    # this, and must never let the search print a reassuring zero.
    out, code = run_collector(cfg, db,
                              import_error=ModuleNotFoundError("No module named 'pysnmp'"))
    check("a range scan with no library exits with the library code",
          code == collector.SNMP_MISSING_EXIT)
    check("it never claims it found 0 new printers",
          "Found 0 new printer" not in out)
    check("nothing was written to the database", snapshots(db) == [])
    check("no discovery file was left behind claiming a scan happened",
          not os.path.exists(collector.discovery_path(db)))


# --------------------------------------------------------------------------- #
# 6. Structural: there is exactly ONE place pysnmp is imported
# --------------------------------------------------------------------------- #

def test_one_import_site():
    src = open(os.path.join(REPO, "collector.py"), encoding="utf-8").read()
    sites = [ln for ln in src.splitlines() if "from pysnmp" in ln or "import pysnmp" in ln]
    check("pysnmp is imported in exactly one place, so the check and the work"
          " cannot disagree", len(sites) == 1)
    body = src.split("def import_snmp(", 1)
    check("...and that place is import_snmp()",
          len(body) == 2 and "from pysnmp" in body[1].split("\ndef ", 1)[0])


def main():
    test_require_snmp()
    test_one_import_site()
    with tempfile.TemporaryDirectory() as tmp:
        test_missing_library_writes_nothing(tmp)
        test_a_genuinely_dead_printer_is_still_offline(tmp)
        test_import_error_during_the_poll(tmp)
        test_discovery_never_says_found_zero(tmp)
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
