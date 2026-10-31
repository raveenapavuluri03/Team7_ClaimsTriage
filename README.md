# Team 7 – Insurance Claims Intake Triage (MSA 8770, Milestone 3)

Prototype workflow that turns a submitted insurance claim (form fields + free-text narrative) into a
validated adjuster review package. **Nothing is paid until a human reviewer approves it.** Milestone 3 adds
a real human checkpoint for payouts, event handling that is safe against duplicates, saved state that
survives a crash, and a production cost model.

**Team:** Raveena Pavuluri, Nikhithareddy Obili, Ishita Varma Kalindindi, Tejaswini Waghmare, Kiarash Rezaei

**Model:** `gpt-oss:20b` running **locally** through the Ollama desktop app. No claim text leaves the machine and
the prototype has no per-token cost. (Ollama also offers cloud models; we did not use them. A cloud or hosted
model would need redaction of personal data first, see the report.)

## Workflow

| Step | Type | What it does | On failure |
|---|---|---|---|
| S1 | Code | Check required form fields | `PAUSED_MISSING_INFO` (ask customer) |
| S2 | Tool/Code | Look up policy record + retrieve relevant policy clauses | `FAILED` (policy not found) |
| S3 | **Model (LLM)** | Extract date, type, amount, police report, vehicle, summary as JSON | API error: 3 attempts with backoff; then `ROUTED_TO_HUMAN` |
| S4 | Code | Schema validation (Pydantic) + cross-check with form (date, amount ±5%) | Invalid JSON: 2 re-prompts; mismatch: `ROUTED_TO_HUMAN` |
| S5 | Code (rules) | Coverage: bind date, limit, deductible, police-report rule | `NOT_COVERED`/`AMBIGUOUS` → `ROUTED_TO_HUMAN` |
| S6 | Code | Build fraud features | – |
| S7 | Model (ML) | Logistic-regression fraud score | Fallback to rule-based score |
| S8 | Code | Check score is between 0 and 1, assign LOW / MEDIUM / HIGH | Fallback score is labelled `UNVERIFIED`, never LOW |
| S9 | Model (LLM) | Explain risk indicators for the adjuster | Template explanation |
| S10 | Code | Save adjuster review package and create a pending review | → `COMPLETED_PENDING_ADJUSTER` |
| H1 | Human (simulated) | Exception review: correct fields (re-validated) / confirm coverage / add info, then resume | – |
| **H2** | **Human (real, M3)** | Reviewer approves or rejects the payout; saved pending state, expiry, escalation | Timeout: escalate, then withhold |
| EXT | External (simulated) | Payment and customer notification services, recorded in SQLite | Check before retry; no duplicate payment |

## Setup

Requires Python 3.10+ and the [Ollama](https://ollama.com/download) desktop app. A 16 GB computer is
recommended for `gpt-oss:20b`.

**1. Get the model**

```bash
ollama pull gpt-oss:20b
```

**2. Install the project**

macOS / Linux:
```bash
git clone https://github.com/raveenapavuluri03/Team7_ClaimsTriage.git
cd Team7_ClaimsTriage
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Windows (Command Prompt):
```cmd
git clone https://github.com/raveenapavuluri03/Team7_ClaimsTriage.git
cd Team7_ClaimsTriage
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

`.env` needs only these two lines (no API key for a local model):
```
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gpt-oss:20b
```

## Run (in this order)

```bash
python test_setup.py           # 1. check LLM connection + dataset
python run_demo.py TC1         # 2. one claim end to end; you act as the reviewer (approve / reject / skip)
python run_m3_tests.py         # 3. Milestone 3 tests: review, duplicates, crash, lost confirmation (~5 min)
python cost_model.py 3000      # 4. production cost per claim and per month at 3000 claims/month
python run_tests.py            # 5. Milestone 2 regression: 19 scenarios (~15 min)
```

Milestone 2 scripts, still available: `make_test_claims.py` (builds `data/test_claims.csv`),
`train_risk_model.py` (ML vs rules), `compare_prompts.py` (Prompt A vs B, ~10 min).

On Windows use `python` instead of `python3` in the commands above.

## Try the human checkpoint yourself

```bash
python run_demo.py TC1                                              # creates a pending review
python review_cli.py list                                           # all reviews and their status
python review_cli.py show R-xxxxxx                                  # what the reviewer sees
python review_cli.py decide R-xxxxxx approve --reviewer adjuster_1 --event-id E-1   # writes inbox/E-1.json
python review_cli.py worker                                         # processes the inbox -> payment recorded
python review_cli.py decide R-xxxxxx approve --reviewer adjuster_1 --event-id E-1   # same event again
python review_cli.py worker                                         # -> DUPLICATE_IGNORED
python review_cli.py ledger                                         # exactly one payment recorded
python review_cli.py sweep --hours 49                               # timer: escalate an undecided review
```

## Human oversight (M3)

| Action | Oversight | Why |
|---|---|---|
| Payout (spends money) | Approval before execution | High stakes, hard to reverse |
| Denial notification | Approval before execution | Affects the customer's claim |
| Request for missing information | Monitoring with intervention | Low stakes, reversible |
| Review package, logs | Later audit | Internal, no external effect |

An approval authorizes **one** payout, for **one** claim and content version (a hash of the reviewed content), until
the review's expiry. Rejection denies the claim. If nobody decides within 48 h the review is escalated to the
supervisor, and after 24 h more the payout is withheld. Nothing is ever paid automatically.

## Claim states (M3)

| From | Event | To |
|---|---|---|
| `COMPLETED_PENDING_ADJUSTER` | reviewer APPROVE (valid, in time) | `APPROVED_EXECUTING` |
| `COMPLETED_PENDING_ADJUSTER` | reviewer REJECT | `DENIED` |
| `COMPLETED_PENDING_ADJUSTER` | 48 h without decision | `ESCALATED_TO_SUPERVISOR` |
| `ESCALATED_TO_SUPERVISOR` | supervisor APPROVE / REJECT | `APPROVED_EXECUTING` / `DENIED` |
| `ESCALATED_TO_SUPERVISOR` | 24 h more without decision | `WITHHELD_EXPIRED` |
| `APPROVED_EXECUTING` | payment confirmed, or found by lookup | `PAID` |
| `APPROVED_EXECUTING` | 3 failed attempts | `PAYOUT_UNCERTAIN` (recovery retries after checking the payment system) |
| any closed state | repeated, late or stale event | no change (logged) |

## Outputs (execution evidence)

| File | Contents |
|---|---|
| `logs/run_log.jsonl` | One line per step and event: timestamp, run id, claim, step, status, attempt, latency, detail |
| `states/<claim>.json` | Full workflow state: inputs, results per step, errors, retry counts, human actions, metrics |
| `outputs/<claim>_package.json` | Adjuster review package |
| `db/triage.db` | SQLite: `reviews`, `events`, `payments`, `notifications` (created at run time, not committed) |
| `results/test_results.csv` | Milestone 2 scenarios: expected vs actual |
| `results/m3_test_results.csv` | Milestone 3 tests: input, expected, actual, evidence |
| `results/m3_reviews.csv`, `m3_events.csv`, `m3_payments.csv`, `m3_notifications.csv` | Database tables after the M3 tests |
| `results/cost_model.csv` | Cost per claim and per month for each deployment option |
| `results/risk_model_comparison.csv` | ML vs rule-based fraud scoring |
| `results/prompt_comparison_summary.csv` | Prompt A vs Prompt B extraction results |

## Project structure

```
review_cli.py       Reviewer / operations commands (list, show, decide, worker, sweep, ledger)
run_demo.py         One claim end to end, then act as the reviewer
run_tests.py        Milestone 2 scenarios (regression)
run_m3_tests.py     Milestone 3 tests
cost_model.py       Production cost model from measured token counts
docs/               Step contracts and state handling (Milestone 2, Tejaswini)
src/
  llm.py          LLM client (Ollama) + latency/token metrics
  data_prep.py    Load and clean the Mendeley dataset
  policies.py     Synthetic policies, policy retrieval (S2), coverage rules (S5)
  extraction.py   Prompts, LLM output validation and cross-check (S3-S4)
  risk.py         Fraud features, ML score, rule-based fallback (S6-S8)
  state.py        Workflow state, atomic persistence, logging
  pipeline.py     Orchestrator: sequence, branches, retries, fallbacks, resume, H1
  store.py        SQLite store for reviews, events and the simulated receivers
  review.py       H2 checkpoint: pending reviews, reviewer view, idempotent events, timeouts, recovery
  external.py     Simulated payment and notification services (with fault injection for tests)
```

## Data

- Mendeley Data `insurance_claims`, Version 2 (A. Aqqad, 2023), DOI 10.17632/992mh7dk9y.2, CC BY 4.0.
- Kaggle `Vehicle Insurance Claim Fraud Detection` (CC0 1.0) was reviewed but not used.
- Policy documents and claim narratives are **synthetic**, created for this project.
- Personal attributes (sex, education, occupation, hobbies, relationship) are excluded from the fraud model.
- No API keys or credentials are stored in this repository. `.env`, `db/` and `inbox/processed/` are not committed.

## Known limitations

- The payment and notification services are simulated (SQLite tables), not real systems.
- The risk model is a small logistic regression on a public dataset; its score is advisory only.
- H1 (exception review) is still simulated through `resume_claim()`; only the H2 payout review is a real checkpoint.
- Test narratives are synthetic and cleaner than real customer text.