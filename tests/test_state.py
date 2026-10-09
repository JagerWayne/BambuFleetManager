"""Thread-safety of the shared per-printer state.

paho callbacks write reports from its network threads while FastAPI handlers
read them on the asyncio loop. These tests hammer both sides at once: the
assertions are about invariants (no lost writes, no torn read-modify-write,
no deadlock through the re-entrant lock), not timing.
"""

import threading

from backend import state


def test_concurrent_writers_all_land():
    state.reset()
    threads = []
    for i in range(8):
        pid = f"p{i}"

        def worker(pid=pid):
            for n in range(200):
                state.record_report(pid, {"gcode_state": "RUNNING", "n": n})
                state.record_homed(pid, True)

        threads.append(threading.Thread(target=worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a writer thread deadlocked"
    assert sorted(state.latest_reports) == sorted(f"p{i}" for i in range(8))
    assert all(v is True for v in state.homed_state.values())


def test_report_and_homed_are_read_as_one_pair():
    """A reader must never see a report from one tick next to another's flag."""
    state.reset()
    state.record_report("p1", {"gcode_state": "IDLE"})
    state.record_homed("p1", True)
    report, homed = state.report_and_homed("p1")
    assert report["gcode_state"] == "IDLE"
    assert homed is True


def test_lock_is_reentrant():
    """set_homed-style nesting (record + save while holding) must not deadlock."""
    state.reset()
    with state.locked():
        with state.locked():
            state.record_homed("p1", True)
            assert state.get_homed("p1") is True


def test_writers_and_readers_interleave_without_tearing():
    """Readers assert an internally consistent pair while writers churn."""
    state.reset()
    stop = threading.Event()
    bad = []

    def writer():
        toggles = [True, False]
        for i in range(3000):
            pid = f"p{i % 3}"
            state.record_report(pid, {"gcode_state": "IDLE", "n": i})
            state.record_homed(pid, toggles[i % 2])
        stop.set()

    def reader():
        while not stop.is_set():
            for pid in ("p0", "p1", "p2"):
                report, homed = state.report_and_homed(pid)
                if report is not None and homed is None:
                    bad.append(pid)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    assert bad == []


def test_reports_with_homed_annotates_every_entry():
    state.reset()
    state.record_report("p1", {"gcode_state": "IDLE"})
    state.record_homed("p1", True)
    annotated = state.reports_with_homed()
    assert annotated["p1"]["homed"] is True
    assert annotated["p1"]["gcode_state"] == "IDLE"


def test_reset_clears_everything():
    state.reset()
    state.record_report("p1", {"gcode_state": "IDLE"})
    state.record_homed("p1", True)
    state.record_ack("p1", "project_file", {"result": "ok"})
    state.reset()
    assert state.latest_reports == {}
    assert state.homed_state == {}
    assert state.last_acks == {}