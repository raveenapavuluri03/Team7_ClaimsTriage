"""Simulated external systems: payment service and customer notification service.
They write to SQLite tables so tests can count what was REALLY recorded as sent.
The payment service honours an idempotency key (as real payment APIs do)."""
from datetime import datetime

from src.store import connect


class PaymentUnavailable(Exception):
    """The payment service did not accept the request (nothing was recorded)."""


class ConfirmationLost(Exception):
    """The payment WAS recorded by the receiver, but the confirmation never reached us."""


class SimulatedCrash(BaseException):
    """Simulates the worker process dying (BaseException so normal error handling cannot catch it)."""


_FAULTS = {}      # fault injection for tests: {"payment": [fault, times_left]}


def inject_fault(target, fault, times=1):
    _FAULTS[target] = [fault, times]


def _take_fault(target):
    f = _FAULTS.get(target)
    if not f:
        return None
    f[1] -= 1
    if f[1] <= 0:
        _FAULTS.pop(target)
    return f[0]


def send_payment(idempotency_key, claim_id, amount):
    fault = _take_fault("payment")
    if fault == "crash_before_send":
        raise SimulatedCrash("worker process died before calling the payment service")
    if fault == "down":
        raise PaymentUnavailable("payment service unavailable (HTTP 503)")
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO payments (idempotency_key, claim_id, amount, created_at) VALUES (?,?,?,?)",
                     (idempotency_key, claim_id, amount, datetime.now().isoformat(timespec="seconds")))
    if fault == "lose_confirmation":
        raise ConfirmationLost("payment recorded, but the response timed out")
    return {"status": "OK", "idempotency_key": idempotency_key}


def lookup_payment(idempotency_key):
    """Ask the receiving system what actually happened (used before any retry)."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    return dict(row) if row else None


def send_notification(idempotency_key, claim_id, kind):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO notifications (idempotency_key, claim_id, kind, created_at) VALUES (?,?,?,?)",
                     (idempotency_key, claim_id, kind, datetime.now().isoformat(timespec="seconds")))


def count(table, claim_id=None):
    with connect() as conn:
        if claim_id:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE claim_id=?", (claim_id,)).fetchone()[0]
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]