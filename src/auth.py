"""Control 2: signed reviewer decisions (who is allowed to trigger a payout).

Before this control, a decision event only NAMED a reviewer ("reviewer": "supervisor_1"), so anyone able to
write a file into inbox/ could claim to be a supervisor. Now every APPROVE / REJECT event must carry an
HMAC-SHA256 signature made with that reviewer's secret key. The worker checks the signature before it
records the event or touches any payment.

PROTOTYPE ONLY: the keys below are made-up test values in a simple role table (no real sign-in system).
In production the keys would live in a secrets manager (or be replaced by company single sign-on) and
never be stored in the repository.
"""
import hashlib
import hmac

USERS = {
    "adjuster_1":   {"role": "adjuster",   "key": "TEST-ONLY-key-adjuster-1"},
    "supervisor_1": {"role": "supervisor", "key": "TEST-ONLY-key-supervisor-1"},
    "clerk_9":      {"role": "clerk",      "key": "TEST-ONLY-key-clerk-9"},
}


def _message(event):
    """The fields that the signature protects. Changing any of them invalidates the signature."""
    return "|".join([str(event.get("event_id")), str(event.get("review_id")), str(event.get("decision")),
                     str(event.get("reviewer")), "1" if event.get("ack_fallback") else "0"])


def sign(event):
    """Return the signature for this event, or None if the reviewer is not in the role table."""
    user = USERS.get(event.get("reviewer"))
    if not user:
        return None
    return hmac.new(user["key"].encode(), _message(event).encode(), hashlib.sha256).hexdigest()


def verify(event):
    """Return (True, 'ok') or (False, reason). Never raises on bad input."""
    user = USERS.get(event.get("reviewer"))
    if not user:
        return False, "unknown reviewer"
    sig = event.get("signature")
    if not isinstance(sig, str) or not sig:
        return False, "missing signature"
    if not hmac.compare_digest(sig, sign(event)):
        return False, "signature does not match"
    return True, "ok"