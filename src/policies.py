"""Synthetic policy corpus, policy retrieval (S2) and deterministic coverage rules (S5)."""
import pandas as pd

# Policy tier is chosen from the policy_csl column in the Mendeley data
POLICY_TIERS = {
    "100/300":  {"tier": "Auto Basic",    "limit": 100_000, "report_threshold": 10_000},
    "250/500":  {"tier": "Auto Standard", "limit": 250_000, "report_threshold": 15_000},
    "500/1000": {"tier": "Auto Premium",  "limit": 500_000, "report_threshold": 25_000},
}

COVERED_TYPES = ["Single Vehicle Collision", "Multi-vehicle Collision", "Vehicle Theft", "Parked Car"]
REPORT_REQUIRED_TYPES = ["Vehicle Theft", "Parked Car"]

POLICY_TEXTS = {
    "Auto Basic": [
        "AUTO BASIC POLICY - MidState Mutual (synthetic document for this project).",
        "Coverage: single vehicle collision, multi-vehicle collision, parked car damage and vehicle theft.",
        "Limit: $100,000 per incident (combined single limit 100/300).",
        "Deductible: the deductible on the policy record is subtracted from every covered payout.",
        "Exclusion: incidents that occur before the policy bind (start) date are not covered.",
        "Documentation: vehicle theft and parked car claims above $10,000 require a police report; otherwise an adjuster must review.",
        "Claims above the per-incident limit are referred to an adjuster.",
    ],
    "Auto Standard": [
        "AUTO STANDARD POLICY - MidState Mutual (synthetic document for this project).",
        "Coverage: single vehicle collision, multi-vehicle collision, parked car damage and vehicle theft.",
        "Limit: $250,000 per incident (combined single limit 250/500).",
        "Deductible: the deductible on the policy record is subtracted from every covered payout.",
        "Exclusion: incidents that occur before the policy bind (start) date are not covered.",
        "Documentation: vehicle theft and parked car claims above $15,000 require a police report; otherwise an adjuster must review.",
        "Claims above the per-incident limit are referred to an adjuster.",
    ],
    "Auto Premium": [
        "AUTO PREMIUM POLICY - MidState Mutual (synthetic document for this project).",
        "Coverage: single vehicle collision, multi-vehicle collision, parked car damage and vehicle theft.",
        "Limit: $500,000 per incident (combined single limit 500/1000).",
        "Deductible: the deductible on the policy record is subtracted from every covered payout.",
        "Exclusion: incidents that occur before the policy bind (start) date are not covered.",
        "Documentation: vehicle theft and parked car claims above $25,000 require a police report; otherwise an adjuster must review.",
        "Claims above the per-incident limit are referred to an adjuster.",
    ],
}


def retrieve_policy(df, policy_number, query="", top_k=3):
    """S2: look up the policy record and retrieve the most relevant policy clauses (keyword retrieval)."""
    rows = df[df["policy_number"] == int(policy_number)]
    if rows.empty:
        return None
    rec = rows.iloc[0]
    tier_info = POLICY_TIERS[rec["policy_csl"]]
    clauses = POLICY_TEXTS[tier_info["tier"]]
    q = set(str(query).lower().split())
    scored = sorted(clauses[1:], key=lambda c: -len(q & set(c.lower().split())))
    return {
        "policy_number": int(rec["policy_number"]),
        "tier": tier_info["tier"],
        "limit": tier_info["limit"],
        "report_threshold": tier_info["report_threshold"],
        "deductible": float(rec["policy_deductable"]),
        "bind_date": pd.Timestamp(rec["policy_bind_date"]).strftime("%Y-%m-%d"),
        "annual_premium": float(rec["policy_annual_premium"]),
        "relevant_clauses": scored[:top_k],
    }


def check_coverage(policy, incident_date, incident_type, amount, police_report):
    """S5: deterministic coverage decision -> COVERED / NOT_COVERED / AMBIGUOUS."""
    reasons = []
    if pd.Timestamp(incident_date) < pd.Timestamp(policy["bind_date"]):
        return {"result": "NOT_COVERED", "payout": 0.0,
                "reasons": ["Incident date is before the policy bind date."]}
    if incident_type not in COVERED_TYPES:
        return {"result": "AMBIGUOUS", "payout": 0.0,
                "reasons": [f"Incident type '{incident_type}' is not a recognised covered type."]}
    result = "COVERED"
    if amount > policy["limit"]:
        result = "AMBIGUOUS"
        reasons.append(f"Claim amount ${amount:,.0f} exceeds the ${policy['limit']:,.0f} limit.")
    if (incident_type in REPORT_REQUIRED_TYPES and police_report != "YES"
            and amount > policy["report_threshold"]):
        result = "AMBIGUOUS"
        reasons.append(f"{incident_type} over ${policy['report_threshold']:,.0f} without a police report.")
    payout = max(0.0, min(amount, policy["limit"]) - policy["deductible"])
    if result == "COVERED":
        reasons.append(f"Covered under {policy['tier']}; payout = claim - deductible.")
    return {"result": result, "payout": round(payout, 2), "reasons": reasons}