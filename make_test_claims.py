"""Builds data/test_claims.csv: synthetic claim submissions (form fields + narrative) with the true values."""
import pandas as pd
from src.data_prep import load_claims
from src.policies import retrieve_policy, check_coverage

df = load_claims()


def fmt_date(d, style):
    d = pd.Timestamp(d)
    day = d.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return [d.strftime("%B %d, %Y"), d.strftime("%m/%d/%Y"), f"the {day}{suffix} of {d.strftime('%B %Y')}"][style % 3]


def fmt_amount(a, style):
    return [f"${a:,.0f}", f"about {a/1000:.1f} thousand dollars", f"{a:.0f} USD"][style % 3]


def police_text(p):
    if p == "YES":
        return "The police came to the scene and filed a report."
    if p == "NO":
        return "I did not call the police."
    return ""


def plural(n, one, many):
    return f"{int(n)} {one if int(n) == 1 else many}"


def narrative(r, style, include_amount=True, amount_override=None):
    amt = amount_override if amount_override is not None else r["total_claim_amount"]
    vehicle = f"my {int(r['auto_year'])} {r['auto_make']} {r['auto_model']}"
    parts = [
        f"On {fmt_date(r['incident_date'], style)}, around {int(r['incident_hour_of_the_day'])}:00, "
        f"{vehicle} was involved in a {str(r['incident_type']).lower()} in {r['incident_city']}, {r['incident_state']}.",
        f"It resulted in {str(r['incident_severity']).lower()}"
        + (f" and {plural(r['bodily_injuries'], 'person was', 'people were')} injured." if r["bodily_injuries"] else "."),
        (f"There {'was' if r['witnesses'] == 1 else 'were'} {plural(r['witnesses'], 'witness', 'witnesses')}."
         if r["witnesses"] else "Nobody else saw what happened."),
        police_text(r["police_report_available"]),
    ]
    if include_amount:
        parts.append(f"I am claiming {fmt_amount(amt, style)} for repairs and related costs.")
    return " ".join(p for p in parts if p)


def truth(r):
    return {
        "true_incident_date": pd.Timestamp(r["incident_date"]).strftime("%Y-%m-%d"),
        "true_incident_type": r["incident_type"],
        "true_amount": float(r["total_claim_amount"]),
        "true_police_report": r["police_report_available"] if pd.notna(r["police_report_available"]) else "UNKNOWN",
        "true_vehicle": f"{int(r['auto_year'])} {r['auto_make']} {r['auto_model']}",
        "fraud_reported": int(r["fraud_reported"]),
    }


def form(r, amount=True):
    return {
        "policy_number": int(r["policy_number"]),
        "form_incident_date": pd.Timestamp(r["incident_date"]).strftime("%Y-%m-%d"),
        "form_claimed_amount": float(r["total_claim_amount"]) if amount else None,
    }


def expected_coverage(r):
    pol = retrieve_policy(df, r["policy_number"])
    pr = r["police_report_available"] if pd.notna(r["police_report_available"]) else "UNKNOWN"
    return check_coverage(pol, r["incident_date"], r["incident_type"], r["total_claim_amount"], pr)["result"]


cases = []
used = set()

# TC1 - clean, covered collision with a police report
tc1 = df[(df["incident_type"].isin(["Single Vehicle Collision", "Multi-vehicle Collision"]))
         & (df["police_report_available"] == "YES") & (df["fraud_reported"] == 0)]
tc1 = tc1[tc1.apply(lambda r: expected_coverage(r) == "COVERED", axis=1)].iloc[0]
used.add(tc1.name)
cases.append({"claim_id": "TC1", "case_type": "Clean covered claim (success path)",
              **form(tc1), "narrative": narrative(tc1, 0), **truth(tc1),
              "expected_status": "COMPLETED_PENDING_ADJUSTER", "force_bad_json": False})

# TC2 - missing claimed amount on the form -> paused for customer
tc2 = df[~df.index.isin(used)].iloc[5]
used.add(tc2.name)
cases.append({"claim_id": "TC2", "case_type": "Missing required field (claimed amount)",
              **form(tc2, amount=False), "narrative": narrative(tc2, 1, include_amount=False), **truth(tc2),
              "expected_status": "PAUSED_MISSING_INFO", "force_bad_json": False})

# TC3 - theft / parked car above threshold with no police report -> AMBIGUOUS coverage -> human review
tc3 = df[(df["incident_type"].isin(["Vehicle Theft", "Parked Car"]))
         & (df["police_report_available"] != "YES") & (~df.index.isin(used))]
tc3 = tc3[tc3.apply(lambda r: expected_coverage(r) != "COVERED", axis=1)]
if tc3.empty:  # fallback: force a high amount on a theft claim
    tc3 = df[(df["incident_type"] == "Vehicle Theft") & (~df.index.isin(used))].copy()
    tc3["total_claim_amount"] = 40_000
    tc3["police_report_available"] = "NO"
tc3 = tc3.iloc[0]
used.add(tc3.name)
cases.append({"claim_id": "TC3", "case_type": "Coverage unclear (no police report) -> human review",
              **form(tc3), "narrative": narrative(tc3, 2), **truth(tc3),
              "expected_status": "ROUTED_TO_HUMAN", "force_bad_json": False})

# TC4 - model returns invalid JSON every time -> retries exhausted -> human review
tc4 = df[~df.index.isin(used)].iloc[10]
used.add(tc4.name)
cases.append({"claim_id": "TC4", "case_type": "Invalid model output (forced) -> retries exhausted",
              **form(tc4), "narrative": narrative(tc4, 0), **truth(tc4),
              "expected_status": "ROUTED_TO_HUMAN", "force_bad_json": True})

# TC5 - narrative amount conflicts with the form amount by >5% -> human review
tc5 = df[~df.index.isin(used)].iloc[20]
used.add(tc5.name)
cases.append({"claim_id": "TC5", "case_type": "Narrative conflicts with form amount",
              **form(tc5), "narrative": narrative(tc5, 1, amount_override=tc5["total_claim_amount"] * 1.6),
              **truth(tc5), "expected_status": "ROUTED_TO_HUMAN", "force_bad_json": False})

# TC6-TC12 - normal mixed claims (used for the prompt A vs B comparison)
others = df[~df.index.isin(used)].sample(7, random_state=7)
for i, (_, r) in enumerate(others.iterrows(), start=6):
    cov = expected_coverage(r)
    cases.append({"claim_id": f"TC{i}", "case_type": "Mixed claim",
                  **form(r), "narrative": narrative(r, i), **truth(r),
                  "expected_status": "COMPLETED_PENDING_ADJUSTER" if cov == "COVERED" else "ROUTED_TO_HUMAN",
                  "force_bad_json": False})

out = pd.DataFrame(cases)
out.to_csv("data/test_claims.csv", index=False)
print(f"Saved {len(out)} test claims to data/test_claims.csv\n")
print(out[["claim_id", "case_type", "policy_number", "form_claimed_amount", "expected_status"]].to_string(index=False))
print("\nExample narrative (TC1):\n", out.loc[0, "narrative"])