"""Prove the dashboard treats printer-supplied text as data, not markup.

Device name, model, IP, status detail and supply description all come straight
from SNMP - a printer sets its own sysName and hrDeviceDescr, so a hostile or
mistyped device can carry HTML or JavaScript. The dashboard embeds those values
in a JSON payload and renders them into the page with innerHTML, so they must be
escaped or the page executes whatever a printer claims to be called.

This renders a deliberately hostile fleet, executes the page in a real browser,
and asserts that nothing injected runs: no payload flag is set, no dialog fires,
no <img>/onerror survives, and the hostile name shows up as inert text. A normal
printer alongside it must still render exactly as before.

    python3 tests/test_xss.py

Needs playwright + a browser (same as the entra-tenant-docs html check).
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import fleetdb  # noqa: E402

# Each payload sets a distinct window flag if it ever executes. If the fix
# holds, none of them run and every flag stays false.
HOSTILE = {
    "name":   '<img src=x onerror=window.__xss_name=1>',
    "model":  '</script><script>window.__xss_model=1</script>',
    "detail": 'Jam"><svg onload=window.__xss_detail=1></svg>',
    "desc":   'Blk"><svg onload=window.__xss_desc=1></svg>',
}


def build_fixture(db):
    conn = fleetdb.connect(db)
    ts = fleetdb.utcnow_iso()
    did = fleetdb.upsert_device(conn, "10.0.0.9", name=HOSTILE["name"],
                                model=HOSTILE["model"], serial="S1", ts=ts)
    fleetdb.insert_snapshot(conn, did, ts, True, "error", HOSTILE["detail"], 100, 5000,
                            [{"slot": 1, "description": HOSTILE["desc"],
                              "supply_type": "toner", "level": 5, "max_capacity": 100}])
    # A normal printer, to prove escaping does not disturb legitimate rendering.
    did2 = fleetdb.upsert_device(conn, "10.0.0.10", name="DFW Front Desk",
                                 model="Canon iR-ADV C5540", serial="S2", ts=ts)
    fleetdb.insert_snapshot(conn, did2, ts, True, "ok", "Idle", 200, 42000,
                            [{"slot": 1, "description": "Black Toner",
                              "supply_type": "toner", "level": 80, "max_capacity": 100}])
    conn.commit()
    conn.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed (pip install playwright; playwright install chromium)")
        return 0

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "fleet.db")
    out = os.path.join(tmp, "fleet.html")
    build_fixture(db)
    subprocess.run([sys.executable, os.path.join(REPO, "dashboard.py"),
                    "--db", db, "--out", out], check=True, capture_output=True)

    dialogs, page_errors = [], []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        pg.on("pageerror", lambda e: page_errors.append(str(e)))
        pg.goto("file://" + out)
        pg.wait_for_timeout(600)
        flags = pg.evaluate(
            "({name:!!window.__xss_name, model:!!window.__xss_model, "
            " detail:!!window.__xss_detail, desc:!!window.__xss_desc})")
        imgs = pg.evaluate("document.querySelectorAll('img').length")
        onerr = pg.evaluate(
            "[...document.querySelectorAll('*')].filter(e=>e.getAttribute"
            "&&e.getAttribute('onerror')).length")
        name_inert = pg.evaluate(
            "[...document.querySelectorAll('.nm')].some(c=>"
            "c.textContent.includes('<img src=x onerror=') && c.querySelector('img')===null)")
        legit = pg.evaluate(
            "document.body.textContent.includes('DFW Front Desk') && "
            "document.body.textContent.includes('Canon iR-ADV C5540')")
        b.close()

    checks = {
        "name payload did not execute":     not flags["name"],
        "model payload did not execute":    not flags["model"],
        "detail payload did not execute":   not flags["detail"],
        "desc payload did not execute":     not flags["desc"],
        "no alert/confirm dialogs fired":   len(dialogs) == 0,
        "no injected <img> elements":       imgs == 0,
        "no element carries onerror":       onerr == 0,
        "hostile name shown as inert text": name_inert,
        "legit printer renders normally":   legit,
        "no uncaught page errors":          len(page_errors) == 0,
    }
    ok = True
    for label, passed in checks.items():
        print(("PASS " if passed else "FAIL ") + label)
        ok = ok and passed
    if page_errors:
        print("page errors:", page_errors)
    print("RESULT:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
