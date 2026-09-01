"""Prove one unresponsive printer can't freeze the whole poll.

Each SNMP request already has its own timeout, but the *entire* device exchange
now has a hard wall-clock ceiling (poll_device's `deadline`). This stands in a
printer that never answers and asserts the poll gives up near the deadline and
raises TimeoutError - which the collector's main loop records as offline - so a
flaky device is a fast non-event, not a hung refresh.

    python3 tests/test_timeout.py
"""

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import collector  # noqa: E402

FAILS = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def test_deadline_bounds_a_hanging_device():
    async def _never_answers(*_a, **_k):
        await asyncio.sleep(30)   # a printer that accepts nothing back

    original = collector.snmp_poll
    collector.snmp_poll = _never_answers
    try:
        t0 = time.time()
        raised = None
        try:
            collector.poll_device("dev", "10.0.0.1", "public", 2, 1, 1, deadline=1.0)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raised = e
        elapsed = time.time() - t0
    finally:
        collector.snmp_poll = original

    check("a hanging device raises TimeoutError", raised is not None)
    check("it gives up near the deadline, not after 30s", elapsed < 5.0)


def test_healthy_device_is_unaffected():
    # A poll that returns promptly must pass straight through the ceiling.
    async def _answers(*_a, **_k):
        return ({}, [])   # normalize() tolerates an empty field set

    original = collector.snmp_poll
    collector.snmp_poll = _answers
    try:
        (status, detail, *_rest), supplies = collector.poll_device(
            "dev", "10.0.0.2", "public", 2, 1, 1, deadline=20.0)
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        print("  unexpected:", e)
    finally:
        collector.snmp_poll = original
    check("a healthy poll passes through the ceiling untouched", ok)


def main():
    test_deadline_bounds_a_hanging_device()
    test_healthy_device_is_unaffected()
    print("")
    print("RESULT:", "%d FAILURES" % len(FAILS) if FAILS else "ALL PASS")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
