"""Milestone 3 tests: human checkpoint, duplicate/late events, crash recovery, uncertain outcomes.
Two cases use the real LLM end to end (approved run, fallback score); the other review-layer cases reuse the
finished extraction of the first run so the whole suite takes about 3-5 minutes.
Writes results/m3_test_results.csv, m3_reviews.csv, m3_payments.csv, m3_events.csv."""
import copy
import os
import uuid
from datetime import timedelta

import pandas as pd

from src import external, review, store
from src.pipeline import resume_claim, run_claim
from src.state import load_state, save_state
from src.store import connect

review.RETRY_BACKOFF_S = 0          # no waiting between simulated payment retries
store.reset()
os.makedirs("results", exist_ok=True)
rows = []


def check(case, test_input, expected, actual, evidence, ok):
    rows.append({"case": case, "input": test_input, "expected": expected, "actual": actual,
                 "evidence": evidence, "pass": bool(ok)})
    print(f"\n>>> {'PASS' if ok else 'FAIL'}  {case}\n    expected: {expected}\n    actual:   {actual}\n    evidence: {evidence}")


def counts(cid):
    return external.count("payments", cid), external.count("notifications", cid)


def ev(cid):
    s = load_state(cid)
    p, n = counts(cid)
    rv = [r for r in review.list_reviews() if r["claim_id"] == cid]
    return f"claim={s['status']}, reviews={[r['status'] for r in rv]}, payments recorded={p}, notifications={n}"


def clone(base_id, new_id, **patch):
    """Copy a finished claim under a new id and give it its own pending review (skips repeated LLM calls)."""
    s = copy.deepcopy(load_state(base_id))
    s.update({"claim_id": new_id, "run_id": uuid.uuid4().hex[:8], "status": "COMPLETED_PENDING_ADJUSTER",
              "human_actions": [], **patch})
    s["input"]["claim_id"] = new_id
    s["results"].pop("EXT", None)
    save_state(s)
    s["results"]["S10"]["review_id"] = review.create_review(s)
    save_state(s)
    return s["results"]["S10"]["review_id"]


def decide(rid, decision, who="adjuster_1", eid=None, ack=False, now=None):
    return review.handle_event(review.make_event(rid, decision, who, "test", eid, ack), now)


claims = pd.read_csv("data/test_claims.csv")
tc1 = claims[claims["claim_id"] == "TC1"].iloc[0].to_dict()
base = {k: v for k, v in tc1.items() if not k.startswith("true_") and k not in
        ("expected_status", "case_type", "fraud_reported", "force_bad_json")}

# ---------------------------------------------------------------- 1. approved run (real LLM, end to end)
st = run_claim({**base, "claim_id": "M3-A"})
rid = st["results"]["S10"]["review_id"]
print(review.render_review(rid))                      # the reviewer's screen (example for the report)
amount = review.get_review(rid)["amount"]
out = decide(rid, "APPROVE")
pay = external.lookup_payment(f"payout:{rid}")
check("1 Approved run", "TC1 narrative -> pipeline -> adjuster_1 approves", "PAID, 1 payment of the proposed amount",
      f"{out}", ev("M3-A") + f", paid ${pay['amount'] if pay else None} vs proposed ${amount}",
      load_state("M3-A")["status"] == "PAID" and pay and pay["amount"] == amount and counts("M3-A") == (1, 1))

# ---------------------------------------------------------------- 2. rejection
rid = clone("M3-A", "M3-B")
out = decide(rid, "REJECT")
check("2 Rejection", "adjuster_1 rejects the review", "DENIED, 0 payments, 1 customer notification",
      out, ev("M3-B"), load_state("M3-B")["status"] == "DENIED" and counts("M3-B") == (0, 1))

# ---------------------------------------------------------------- 3. timeout, escalation, late approval
rid = clone("M3-A", "M3-C")
t0 = pd.Timestamp(review.get_review(rid)["created_at"]).to_pydatetime()
review.sweep(t0 + timedelta(hours=49))
s1 = load_state("M3-C")["status"]
o_adj = decide(rid, "APPROVE", "adjuster_1", now=t0 + timedelta(hours=50))      # adjuster no longer owns it
review.sweep(t0 + timedelta(hours=73))
s2 = load_state("M3-C")["status"]
o_late = decide(rid, "APPROVE", "supervisor_1", now=t0 + timedelta(hours=74))   # approval after expiry
check("3a Timeout -> escalation -> withheld", "no decision for 49 h, then 73 h",
      "ESCALATED_TO_SUPERVISOR, then WITHHELD_EXPIRED, 0 payments",
      f"{s1} -> {s2}", ev("M3-C"), s1 == "ESCALATED_TO_SUPERVISOR" and s2 == "WITHHELD_EXPIRED" and counts("M3-C")[0] == 0)
check("3b Late / unauthorized approval", "adjuster_1 approves while escalated; supervisor approves after expiry",
      "NOT_AUTHORIZED, then LATE_IGNORED(EXPIRED); still 0 payments", f"{o_adj}, {o_late}", ev("M3-C"),
      o_adj == "NOT_AUTHORIZED" and o_late == "LATE_IGNORED(EXPIRED)" and counts("M3-C")[0] == 0)

# ---------------------------------------------------------------- 4. duplicate events and conflicting repeats
rid = clone("M3-A", "M3-D")
o1 = decide(rid, "APPROVE", eid="E-DUP-1")
o2 = decide(rid, "APPROVE", eid="E-DUP-1")            # same event delivered twice (same event_id)
o3 = decide(rid, "APPROVE", eid="E-DUP-2")            # second click: new event_id, same review
o4 = decide(rid, "REJECT", eid="E-DUP-3")             # conflicting decision after approval
check("4 Duplicate event / retry", "approve delivered twice, approved again, then rejected afterwards",
      "1 payment only; repeats ignored",
      f"{o1}; {o2}; {o3}; {o4}", ev("M3-D"),
      (o2, o3, o4) == ("DUPLICATE_IGNORED", "LATE_IGNORED(APPROVED)", "LATE_IGNORED(APPROVED)")
      and counts("M3-D") == (1, 1) and load_state("M3-D")["status"] == "PAID")
before = load_state("M3-D")["status"]
resume_claim("M3-D", "adjuster_1", "late H1 request after the claim is closed")
check("4b Late resume of a closed claim", "resume_claim() on a PAID claim", "ignored, status unchanged, still 1 payment",
      load_state("M3-D")["status"], ev("M3-D"), before == "PAID" == load_state("M3-D")["status"] and counts("M3-D")[0] == 1)

# ---------------------------------------------------------------- 5. crash after approval, before the payment call
rid = clone("M3-A", "M3-E")
external.inject_fault("payment", "crash_before_send")
try:
    decide(rid, "APPROVE", eid="E-CRASH-1")
    crashed = False
except external.SimulatedCrash:
    crashed = True
mid = f"claim={load_state('M3-E')['status']}, review={review.get_review(rid)['status']}, payments={counts('M3-E')[0]}"
review.process_inbox()                                 # worker restart: recover() finishes the saved approval
o_redeliver = decide(rid, "APPROVE", eid="E-CRASH-1")  # the broker redelivers the same event
check("5 Interruption and resume", "worker dies after approval is saved but before the payment call; restart",
      "after crash: APPROVED_EXECUTING, 0 payments; after restart: PAID, exactly 1 payment",
      f"crash: {mid}; restart: {load_state('M3-E')['status']}; redelivered event: {o_redeliver}", ev("M3-E"),
      crashed and "APPROVED_EXECUTING" in mid and load_state("M3-E")["status"] == "PAID" and counts("M3-E") == (1, 1))

# ---------------------------------------------------------------- 6. uncertain outcome: payment made, confirmation lost
rid = clone("M3-A", "M3-F")
external.inject_fault("payment", "lose_confirmation")
out = decide(rid, "APPROVE")
log_has = "not resent" in load_state("M3-F")["status_reason"]
check("6 Uncertain outcome", "payment service records the payment but the response is lost",
      "workflow checks the payment system, marks PAID, does NOT send again (1 payment)", out,
      ev("M3-F") + f", reason: {load_state('M3-F')['status_reason']}",
      load_state("M3-F")["status"] == "PAID" and log_has and counts("M3-F") == (1, 1))

# ---------------------------------------------------------------- 7. payment service down, then back
rid = clone("M3-A", "M3-G")
external.inject_fault("payment", "down", times=3)
out = decide(rid, "APPROVE")
s_down = load_state("M3-G")["status"]
p_down = counts("M3-G")[0]
review.recover()                                       # service is back: operations re-run recovery
check("7 Payment service unavailable", "3 failed payment attempts, then the service recovers",
      "PAYOUT_UNCERTAIN with 0 payments, then PAID with exactly 1 after recovery",
      f"{s_down} (payments {p_down}) -> {load_state('M3-G')['status']}", ev("M3-G"),
      s_down == "PAYOUT_UNCERTAIN" and p_down == 0 and load_state("M3-G")["status"] == "PAID" and counts("M3-G")[0] == 1)

# ---------------------------------------------------------------- 8. fallback risk score (real LLM, model failure forced)
st = run_claim({**base, "claim_id": "M3-H"}, force_model_fail=True)
rid = st["results"]["S10"]["review_id"]
band = st["results"]["S7"]["band"]
print(review.render_review(rid))
o_noack = decide(rid, "APPROVE")
p_noack = counts("M3-H")[0]
o_ack = decide(rid, "APPROVE", ack=True)
check("8 Fallback risk score", "risk model fails; fallback scores the claim",
      "band UNVERIFIED (not LOW); approval without acknowledgement refused; with it PAID",
      f"band={band}; {o_noack}; {o_ack}", ev("M3-H"),
      band == "UNVERIFIED" and o_noack == "NEEDS_ACK_FALLBACK" and p_noack == 0 and load_state("M3-H")["status"] == "PAID")

# ---------------------------------------------------------------- 9. reviewer correction is re-validated; approval binds to the corrected version
rid1 = clone("M3-A", "M3-I")
form_amt = float(load_state("M3-I")["input"]["form_claimed_amount"])
resume_claim("M3-I", "adjuster_1", "wrong amount typed by reviewer", corrected_fields={"amount": round(form_amt * 1.5, 2)})
refused = review.get_review(rid1)["status"] == "PENDING" and any("REFUSED" in a["action"] for a in load_state("M3-I")["human_actions"])
resume_claim("M3-I", "adjuster_1", "vehicle model was wrong", corrected_fields={"vehicle": "2012 Corrected Make"})
rid2 = load_state("M3-I")["results"]["S10"]["review_id"]
o_old = decide(rid1, "APPROVE", eid="E-OLD")
o_new = decide(rid2, "APPROVE", eid="E-NEW")
pay = external.lookup_payment(f"payout:{rid2}")
check("9 Corrected claim re-validated", "bad correction (amount +50%), then valid correction, then approve old and new review",
      "bad correction refused; old review SUPERSEDED and cannot pay; new approval pays once",
      f"refused={refused}; old: {o_old}; new: {o_new}", ev("M3-I") + f", paid under review {rid2}: {bool(pay)}",
      refused and review.get_review(rid1)["status"] == "SUPERSEDED" and o_old == "LATE_IGNORED(SUPERSEDED)"
      and counts("M3-I")[0] == 1 and bool(pay))

# ---------------------------------------------------------------- 10. resume resets retry counters (reviewer resumes a routed claim)
rid = clone("M3-A", "M3-J", status="ROUTED_TO_HUMAN", current_step="S5", retries={"S3_reprompt": 2})
resume_claim("M3-J", "adjuster_1", "coverage confirmed by phone", override_coverage=True)
sj = load_state("M3-J")
check("10 Retry counters reset on resume", "claim stopped with retries={'S3_reprompt': 2}; reviewer resumes it",
      "retries cleared, old counters kept in retry_history, new review pending",
      f"retries={sj['retries']}, history={sj.get('retry_history')}", f"claim={sj['status']}",
      sj["retries"] == {} and {"S3_reprompt": 2} in sj.get("retry_history", []) and sj["status"] == "COMPLETED_PENDING_ADJUSTER")

# ---------------------------------------------------------------- 11. unexpected events
rid = clone("M3-A", "M3-K")
o_unknown = decide("R-DOESNOTEXIST", "APPROVE")
o_type = review.handle_event({"event_id": "E-ODD", "type": "SOMETHING_ELSE", "review_id": rid})
o_who = decide(rid, "APPROVE", "clerk_9")
o_noid = review.handle_event({"type": "REVIEW_DECISION", "review_id": rid, "decision": "APPROVE", "reviewer": "adjuster_1"})
check("11 Unexpected events", "unknown review, unknown event type, unauthorized reviewer, event without id",
      "UNKNOWN_REVIEW, UNEXPECTED_EVENT, NOT_AUTHORIZED, INVALID_EVENT; 0 payments",
      f"{o_unknown}, {o_type}, {o_who}, {o_noid}", ev("M3-K"),
      (o_unknown, o_type, o_who, o_noid) == ("UNKNOWN_REVIEW", "UNEXPECTED_EVENT", "NOT_AUTHORIZED", "INVALID_EVENT")
      and counts("M3-K")[0] == 0)

# ---------------------------------------------------------------- summary + exports
df = pd.DataFrame(rows)
df.to_csv("results/m3_test_results.csv", index=False)
with connect() as c:
    for table, name in (("reviews", "m3_reviews"), ("payments", "m3_payments"), ("events", "m3_events"), ("notifications", "m3_notifications")):
        pd.read_sql_query(f"SELECT * FROM {table}", c).to_csv(f"results/{name}.csv", index=False)
print("\n================ M3 TEST SUMMARY ================")
print(df[["case", "pass"]].to_string(index=False))
print(f"\nPassed {int(df['pass'].sum())}/{len(df)}")
print(f"Total payments recorded: {external.count('payments')} (expected 1 per paid claim)")
print("Saved: results/m3_test_results.csv, m3_reviews.csv, m3_payments.csv, m3_events.csv, m3_notifications.csv")