"""H2: the real human checkpoint for payouts.

Covers: creating a pending review (saved in SQLite), what the reviewer sees, what an approval authorizes,
idempotent event handling (duplicate / late / stale / unauthorized events), timeout + escalation,
safe execution of the external payment (check before retry), and recovery after a crash."""
import glob
import hashlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta

from src import external
from src.state import load_state, log_raw, log_step, now as ts_now, save_state
from src.store import connect

REVIEW_TTL_HOURS = 48          # reviewer must decide within 48 h ...
ESCALATION_TTL_HOURS = 24      # ... then the supervisor gets 24 h more ...
MAX_PAY_ATTEMPTS = 3           # payment attempts per approval
RETRY_BACKOFF_S = 1.0          # 1 s, 2 s between payment attempts (tests set this to 0)
ADJUSTER = "adjuster_1"        # default assigned reviewer
SUPERVISOR = "supervisor_1"    # escalation owner
OPEN = ("PENDING", "ESCALATED")
INBOX = "inbox"
ML_METHOD = "ml_logistic_regression"


def _t(now):
    return now or datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------------ content version
def content_hash(state):
    """Fingerprint of everything the reviewer approves. If the claim content changes, the hash changes
    and an older approval no longer applies."""
    r = state["results"]
    payload = {"claim_id": state["claim_id"], "policy": r["S2"]["policy_number"],
               "extracted": r["S3"]["extracted"], "form_amount": float(state["input"]["form_claimed_amount"]),
               "coverage": r["S5"]["result"], "payout": r["S5"]["payout"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ create / read
def create_review(state, now=None):
    """Called at the end of the workflow (S10). Saves a PENDING review; any older open review for the same
    claim is SUPERSEDED, so an approval can only ever apply to the latest (corrected) version."""
    t = _t(now)
    res = state["results"]
    review_id = f"R-{uuid.uuid4().hex[:6]}"
    fallback = int(res["S7"]["method"] != ML_METHOD)
    with connect() as conn:
        conn.execute("UPDATE reviews SET status='SUPERSEDED' WHERE claim_id=? AND status IN ('PENDING','ESCALATED')",
                     (state["claim_id"],))
        conn.execute(
            "INSERT INTO reviews (review_id, claim_id, run_id, status, action, amount, content_hash, assigned_to,"
            " escalate_to, fallback_score, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (review_id, state["claim_id"], state["run_id"], "PENDING", "PAYOUT", res["S5"]["payout"],
             content_hash(state), ADJUSTER, SUPERVISOR, fallback, _iso(t),
             _iso(t + timedelta(hours=REVIEW_TTL_HOURS))))
    log_step(state, "H2", "pending", f"review {review_id} created: PAYOUT ${res['S5']['payout']:,.0f}, "
                                     f"expires in {REVIEW_TTL_HOURS} h, fallback_score={bool(fallback)}")
    return review_id


def get_review(review_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
    return dict(row) if row else None


def open_review_for(claim_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE claim_id=? AND status IN ('PENDING','ESCALATED') "
                           "ORDER BY created_at DESC LIMIT 1", (claim_id,)).fetchone()
    return dict(row) if row else None


def list_reviews(status=None):
    with connect() as conn:
        q = "SELECT * FROM reviews" + (" WHERE status=?" if status else "") + " ORDER BY created_at"
        rows = conn.execute(q, (status,) if status else ()).fetchall()
    return [dict(r) for r in rows]


def conflicts(state):
    """Things the reviewer must see before approving."""
    r, out = state["results"], []
    risk, ex = r["S7"], r["S3"]["extracted"]
    if risk["method"] != ML_METHOD:
        out.append("RISK SCORE IS A FALLBACK (rule-based). The risk model did not run, so the score is NOT "
                   "reliable and the band is shown as UNVERIFIED.")
    if risk.get("band") == "HIGH":
        out.append("HIGH fraud risk, but coverage rules say COVERED.")
    if ex["police_report"] != "YES":
        out.append(f"Police report: {ex['police_report']}.")
    if r["S3"].get("corrected_by"):
        out.append(f"Extracted fields were corrected by {r['S3']['corrected_by']} and re-validated.")
    for issue in r.get("S4", {}).get("issues", []):
        out.append(issue)
    return out


def render_review(review_id):
    """What the reviewer sees (text form of a simple review screen)."""
    rv = get_review(review_id)
    st = load_state(rv["claim_id"])
    r, ex, pol, cov, risk = st["results"], st["results"]["S3"]["extracted"], st["results"]["S2"], st["results"]["S5"], st["results"]["S7"]
    cf = conflicts(st)
    band = risk.get("band")
    lines = [
        "=" * 70,
        f"REVIEW {rv['review_id']}   status: {rv['status']}   claim: {rv['claim_id']}",
        f"Assigned to: {rv['assigned_to']}   Escalation owner: {rv['escalate_to']}   Expires: {rv['expires_at']}",
        "-" * 70,
        f"PROPOSED ACTION : {rv['action']} of ${rv['amount']:,.2f} to policy {pol['policy_number']} ({pol['tier']})",
        f"CONTENT VERSION : {rv['content_hash']}",
        "EVIDENCE",
        f"  Incident      : {ex['incident_type']} on {ex['incident_date']}; police report {ex['police_report']}",
        f"  Claimed amount: ${float(st['input']['form_claimed_amount']):,.2f}  (limit ${pol['limit']:,}, deductible ${pol['deductible']:,.0f})",
        f"  Risk          : score {risk['score']}, band {band}, method {risk['method']}",
        f"  Explanation   : {r['S9']['explanation']}",
        "APPLICABLE RULE",
        f"  Coverage {cov['result']}: {' '.join(cov['reasons'])}",
        f"  Policy clause : {pol['relevant_clauses'][0] if pol['relevant_clauses'] else '-'}",
        "CONFLICTS / WARNINGS",
    ]
    lines += [f"  ! {c}" for c in cf] or ["  none"]
    lines += [
        "IF YOU APPROVE this authorizes ONLY:",
        f"  action {rv['action']} | target claim {rv['claim_id']} | amount ${rv['amount']:,.2f} | "
        f"version {rv['content_hash']} | valid until {rv['expires_at']} | one payment (key payout:{rv['review_id']})",
    ]
    if rv["fallback_score"]:
        lines.append("  You must acknowledge the fallback risk score to approve (ack_fallback).")
    lines += ["IF YOU REJECT : no payment; claim becomes DENIED; customer is notified once.",
              "IF NOBODY DECIDES: escalated to the supervisor after the expiry; if still undecided, the payout "
              "is withheld (WITHHELD_EXPIRED). Nothing is ever paid automatically.", "=" * 70]
    return "\n".join(lines)


# ------------------------------------------------------------------ timeouts and escalation
def _sweep_one(review_id, t):
    """Apply expiry rules to one review (may escalate, then expire if far overdue)."""
    while True:
        rv = get_review(review_id)
        if not rv or rv["status"] not in OPEN or t < datetime.fromisoformat(rv["expires_at"]):
            return rv
        exp = datetime.fromisoformat(rv["expires_at"])
        st = load_state(rv["claim_id"])
        if rv["status"] == "PENDING":
            with connect() as conn:
                conn.execute("UPDATE reviews SET status='ESCALATED', escalated_at=?, expires_at=? WHERE review_id=? AND status='PENDING'",
                             (_iso(exp), _iso(exp + timedelta(hours=ESCALATION_TTL_HOURS)), review_id))
            st["status"], st["status_reason"] = "ESCALATED_TO_SUPERVISOR", f"No decision in {REVIEW_TTL_HOURS} h; escalated to {rv['escalate_to']}."
            log_step(st, "H2", "escalated", st["status_reason"])
        else:
            with connect() as conn:
                conn.execute("UPDATE reviews SET status='EXPIRED' WHERE review_id=? AND status='ESCALATED'", (review_id,))
            st["status"], st["status_reason"] = "WITHHELD_EXPIRED", "Review expired after escalation. Payout withheld; case needs a manual decision."
            log_step(st, "H2", "expired", st["status_reason"])
        save_state(st)


def sweep(now=None):
    """Timer event: escalate or expire reviews that passed their deadline."""
    t = _t(now)
    for rv in list_reviews():
        if rv["status"] in OPEN:
            _sweep_one(rv["review_id"], t)


# ------------------------------------------------------------------ events
def make_event(review_id, decision, reviewer, note="", event_id=None, ack_fallback=False):
    return {"event_id": event_id or f"E-{uuid.uuid4().hex[:8]}", "type": "REVIEW_DECISION", "review_id": review_id,
            "decision": decision.upper(), "reviewer": reviewer, "note": note, "ack_fallback": ack_fallback}


def drop_event_file(event, name=None):
    """Event source: a reviewer decision is written as a JSON file into inbox/ (simulated webhook)."""
    os.makedirs(INBOX, exist_ok=True)
    path = os.path.join(INBOX, name or f"{event['event_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(event, f, indent=2)
    return path


def handle_event(event, now=None):
    """Process one incoming event exactly once in effect. Returns the outcome string."""
    t = _t(now)
    eid = event.get("event_id")
    if not eid:
        log_raw(event.get("review_id", "?"), "-", "EVT", "invalid", "event without event_id")
        return "INVALID_EVENT"
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO events (event_id, event_type, review_id, claim_id, payload, received_at) "
                           "VALUES (?,?,?,?,?,?)", (eid, event.get("type"), event.get("review_id"), None,
                                                    json.dumps(event), _iso(t)))
        if cur.rowcount == 0:
            done = conn.execute("SELECT outcome FROM events WHERE event_id=?", (eid,)).fetchone()["outcome"]
            if done is not None:                      # already fully processed -> ignore the repeat
                log_raw(event.get("review_id", "?"), "-", "EVT", "duplicate", f"{eid} already processed ({done}); ignored")
                return "DUPLICATE_IGNORED"
            # received earlier but the worker died before finishing -> process it again (effects are idempotent)
    outcome = _process(event, t)
    with connect() as conn:
        conn.execute("UPDATE events SET outcome=?, claim_id=(SELECT claim_id FROM reviews WHERE review_id=?) WHERE event_id=?",
                     (outcome, event.get("review_id"), eid))
    return outcome


def _process(event, t):
    rid = event.get("review_id")
    if event.get("type") != "REVIEW_DECISION" or event.get("decision") not in ("APPROVE", "REJECT"):
        log_raw(rid, "-", "EVT", "unexpected", f"unsupported event {event.get('type')}/{event.get('decision')}")
        return "UNEXPECTED_EVENT"
    rv = _sweep_one(rid, t) if get_review(rid) else None       # timers are applied first
    if rv is None:
        log_raw(rid, "-", "EVT", "unknown", "no such review")
        return "UNKNOWN_REVIEW"
    st = load_state(rv["claim_id"])
    who = event.get("reviewer")
    if rv["status"] not in OPEN:
        log_step(st, "EVT", "late", f"{event['event_id']}: review {rid} is already {rv['status']}; no action taken")
        return f"LATE_IGNORED({rv['status']})"
    allowed = {rv["assigned_to"], rv["escalate_to"]} if rv["status"] == "PENDING" else {rv["escalate_to"]}
    if who not in allowed:
        log_step(st, "EVT", "refused", f"{who} may not decide review {rid} while it is {rv['status']}")
        return "NOT_AUTHORIZED"
    if content_hash(st) != rv["content_hash"]:
        with connect() as conn:
            conn.execute("UPDATE reviews SET status='SUPERSEDED' WHERE review_id=? AND status IN ('PENDING','ESCALATED')", (rid,))
        log_step(st, "EVT", "refused", f"review {rid} is for an older version of the claim; approval not applied")
        return "STALE_VERSION"
    decision = event["decision"]
    if decision == "APPROVE" and rv["fallback_score"] and not event.get("ack_fallback"):
        log_step(st, "EVT", "refused", "approval needs ack_fallback: the risk score is a fallback")
        return "NEEDS_ACK_FALLBACK"
    new_status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    with connect() as conn:                                     # compare-and-set: only one decision can win
        cur = conn.execute("UPDATE reviews SET status=?, decided_by=?, decided_at=?, decision_note=? "
                           "WHERE review_id=? AND status IN ('PENDING','ESCALATED')",
                           (new_status, who, _iso(t), event.get("note", ""), rid))
        if cur.rowcount == 0:
            return "LATE_IGNORED(RACE)"
    st["human_actions"].append({"ts": ts_now(), "reviewer": who, "action": decision, "review_id": rid,
                                "content_version": rv["content_hash"], "note": event.get("note", "")})
    log_step(st, "H2", "human", f"{who}: {decision} review {rid} (version {rv['content_hash']}) - {event.get('note', '')}")
    if decision == "REJECT":
        _finish_rejected(st, rv)
        return "REJECTED"
    st["status"], st["status_reason"] = "APPROVED_EXECUTING", "Approved; payment being sent."
    save_state(st)                                              # saved BEFORE calling the payment system
    return "APPROVED_" + execute_payout(rv["claim_id"])


def _finish_rejected(st, rv):
    st["status"], st["status_reason"] = "DENIED", f"Payout rejected by {rv['decided_by'] or 'reviewer'}; no payment made."
    save_state(st)
    external.send_notification(f"notify:{rv['review_id']}:DENIED", st["claim_id"], "DENIED")
    log_step(st, "EXT", "mock", "Notification service (simulated): customer informed of DENIED")


# ------------------------------------------------------------------ safe execution of the external action
def _mark_paid(st, rv, key, how):
    st["status"], st["status_reason"] = "PAID", how
    st["results"]["EXT"] = {"payment_key": key, "amount": rv["amount"], "confirmed_at": ts_now()}
    save_state(st)
    external.send_notification(f"notify:{rv['review_id']}:PAID", st["claim_id"], "PAID")
    log_step(st, "EXT", "ok", how)


def execute_payout(claim_id):
    """Send the authorized payment once. Before any send or retry we ask the payment system what really happened."""
    st = load_state(claim_id)
    if st["status"] not in ("APPROVED_EXECUTING", "PAYOUT_UNCERTAIN"):
        return st["status"]
    with connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE claim_id=? AND status='APPROVED' ORDER BY decided_at DESC LIMIT 1",
                           (claim_id,)).fetchone()
    rv, key = dict(row), f"payout:{row['review_id']}"
    found = external.lookup_payment(key)
    if found:
        _mark_paid(st, rv, key, "Payment already recorded by the payment system (checked first); not sent again.")
        return "PAID"
    for attempt in range(1, MAX_PAY_ATTEMPTS + 1):
        try:
            external.send_payment(key, claim_id, rv["amount"])
            _mark_paid(st, rv, key, f"Payment of ${rv['amount']:,.2f} confirmed (attempt {attempt}).")
            return "PAID"
        except external.PaymentUnavailable as e:
            log_step(st, "EXT", "api_error", e, attempt)
            time.sleep(RETRY_BACKOFF_S * 2 ** (attempt - 1))
        except external.ConfirmationLost as e:
            log_step(st, "EXT", "uncertain", f"{e}; checking what actually happened", attempt)
            if external.lookup_payment(key):
                _mark_paid(st, rv, key, "Confirmation was lost, but the payment system shows it was recorded; not resent.")
                return "PAID"
    st["status"], st["status_reason"] = "PAYOUT_UNCERTAIN", "Payment not confirmed after retries; operations must check the payment system."
    save_state(st)
    log_step(st, "EXT", "fail", st["status_reason"])
    return "UNCERTAIN"


# ------------------------------------------------------------------ recovery and the inbox worker
def recover():
    """Run at worker start-up: finish decisions that were saved but not fully applied before a crash."""
    with connect() as conn:
        done = [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE status IN ('APPROVED','REJECTED','EXPIRED')")]
    for rv in done:
        if not os.path.exists(os.path.join("states", f"{rv['claim_id']}.json")):
            continue
        st = load_state(rv["claim_id"])
        if rv["status"] == "APPROVED":
            if st["status"] in ("COMPLETED_PENDING_ADJUSTER", "ESCALATED_TO_SUPERVISOR"):
                st["status"] = "APPROVED_EXECUTING"
                save_state(st)
            if st["status"] in ("APPROVED_EXECUTING", "PAYOUT_UNCERTAIN"):
                log_step(st, "REC", "recover", f"resuming payout for approved review {rv['review_id']}")
                execute_payout(rv["claim_id"])
        elif rv["status"] == "REJECTED" and st["status"] in ("COMPLETED_PENDING_ADJUSTER", "ESCALATED_TO_SUPERVISOR"):
            log_step(st, "REC", "recover", f"completing rejection of review {rv['review_id']}")
            _finish_rejected(st, rv)


def process_inbox(now=None):
    """Event worker: recover first, then handle every file in inbox/ and move it to inbox/processed/."""
    recover()
    results = []
    os.makedirs(os.path.join(INBOX, "processed"), exist_ok=True)
    for path in sorted(glob.glob(os.path.join(INBOX, "*.json"))):
        with open(path, encoding="utf-8") as f:
            event = json.load(f)
        results.append((os.path.basename(path), handle_event(event, now)))
        shutil.move(path, os.path.join(INBOX, "processed", f"{int(time.time() * 1000)}_{os.path.basename(path)}"))
    return results