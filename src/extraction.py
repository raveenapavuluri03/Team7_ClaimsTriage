"""S3 (LLM extraction) and S4 (validation + cross-check) for claim narratives."""
import json
import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, field_validator

INCIDENT_TYPES = ["Single Vehicle Collision", "Multi-vehicle Collision", "Vehicle Theft", "Parked Car", "Other"]


class ExtractedClaim(BaseModel):
    """Schema the model output must satisfy (S4 validation)."""
    incident_date: str
    incident_type: Literal["Single Vehicle Collision", "Multi-vehicle Collision",
                           "Vehicle Theft", "Parked Car", "Other"]
    amount: Optional[float] = None
    police_report: Literal["YES", "NO", "UNKNOWN"]
    vehicle: Optional[str] = None
    summary: str

    @field_validator("incident_date")
    @classmethod
    def valid_date(cls, v):
        d = date.fromisoformat(v)               # raises if not YYYY-MM-DD
        if d > date.today():
            raise ValueError("incident_date is in the future")
        return v

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, v):
        if v is not None and v <= 0:
            raise ValueError("amount must be greater than 0")
        return v


# Prompt A: short, basic instruction (baseline for the comparison)
PROMPT_A = """Read this insurance claim and extract the incident date, incident type, claimed amount,
whether there is a police report, the vehicle, and a one-sentence summary. Answer in JSON.

Claim:
{narrative}"""

# Prompt B: explicit schema, allowed values, rules and one example (used in the pipeline)
PROMPT_B = """You are a claims intake assistant. Extract structured fields from the customer's claim narrative.

Return ONLY a JSON object with exactly these keys:
- "incident_date": string, format YYYY-MM-DD
- "incident_type": one of "Single Vehicle Collision", "Multi-vehicle Collision", "Vehicle Theft", "Parked Car", "Other"
- "amount": number in US dollars with no symbols or commas, or null if not stated
- "police_report": "YES" if a police report was filed, "NO" if the police were not called, otherwise "UNKNOWN"
- "vehicle": "year make model" as written, or null
- "summary": one neutral sentence describing what happened

Rules: use only facts stated in the narrative; do not guess missing values; "about 12.5 thousand dollars" = 12500.

Example
Narrative: "On March 3, 2015 my 2010 Honda Civic was hit while parked in Dayton, OH. Minor damage. I did not call the police. I am claiming about 4.2 thousand dollars."
Output: {{"incident_date": "2015-03-03", "incident_type": "Parked Car", "amount": 4200, "police_report": "NO", "vehicle": "2010 Honda Civic", "summary": "A parked 2010 Honda Civic was hit in Dayton, OH, causing minor damage."}}

Narrative:
{narrative}"""

PROMPTS = {"A": PROMPT_A, "B": PROMPT_B}

# Field-name aliases so Prompt A's free-form keys can still be scored fairly
ALIASES = {"date": "incident_date", "incident date": "incident_date", "type": "incident_type",
           "incident type": "incident_type", "claimed_amount": "amount", "claim_amount": "amount",
           "claimed amount": "amount", "police report": "police_report", "policereport": "police_report"}


def build_prompt(narrative, version="B", feedback=None):
    prompt = PROMPTS[version].format(narrative=narrative)
    if feedback:
        prompt += ("\n\nYour previous answer was rejected because: " + feedback +
                   "\nReturn ONLY a corrected JSON object that follows the rules exactly.")
    return prompt


def parse_and_validate(text):
    """S4: parse the raw model text and validate it against the schema.
    Returns (validated_dict, None) or (None, error_message)."""
    if not text or not text.strip():
        return None, "empty response"
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e.msg})"
    if not isinstance(data, dict):
        return None, "JSON is not an object"
    data = {ALIASES.get(k.lower(), k.lower()): v for k, v in data.items()}
    if isinstance(data.get("amount"), str):
        num = re.sub(r"[^\d.]", "", data["amount"])
        data["amount"] = float(num) if num else None
    if isinstance(data.get("police_report"), bool):
        data["police_report"] = "YES" if data["police_report"] else "NO"
    if isinstance(data.get("police_report"), str):
        data["police_report"] = data["police_report"].upper()
    try:
        return ExtractedClaim(**data).model_dump(), None
    except ValidationError as e:
        msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        return None, "schema validation failed: " + "; ".join(msgs)


def cross_check(extracted, form, tolerance=0.05):
    """S4 consistency check: compare the extracted fields with the submitted form fields.
    Returns a list of issues (empty list = consistent)."""
    issues = []
    if form.get("form_incident_date") and extracted["incident_date"] != form["form_incident_date"]:
        issues.append(f"Date mismatch: narrative {extracted['incident_date']} vs form {form['form_incident_date']}")
    fa, ea = form.get("form_claimed_amount"), extracted.get("amount")
    if isinstance(fa, float) and fa != fa:      # NaN from a CSV means "missing"
        fa = None
    if fa and ea is not None and abs(ea - fa) / fa > tolerance:
        issues.append(f"Amount mismatch: narrative ${ea:,.0f} vs form ${fa:,.0f} (>{tolerance:.0%})")
    return issues