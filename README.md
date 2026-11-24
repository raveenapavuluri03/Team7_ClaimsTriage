# Team 7 – Insurance Claims Intake Triage (MSA 8770, Milestone 4)

Prototype workflow that turns a submitted insurance claim (form fields + free-text narrative) into a
validated adjuster review package. **Nothing is paid until a human reviewer approves it.** Milestone 3 added
a real human checkpoint for payouts, event handling that is safe against duplicates, saved state that
survives a crash, and a production cost model. **Milestone 4 adds two security controls and their tests.**

**Team:** Raveena Pavuluri, Nikhithareddy Obili, Ishita Varma Kalindindi, Tejaswini Waghmare, Kiarash Rezaei

**Model:** `gpt-oss:20b` running **locally** through the Ollama desktop app. No claim text leaves the machine and
the prototype has no per-token cost. (Ollama also offers cloud models; we did not use them. A cloud or hosted
model would need redaction of personal data first, see the Milestone 3 report.)

## Workflow

| Step | Type | What it does | On failure |
|---|---|---|---|
| S1 | Code | Check required form fields; **narrative guard (M4 control 1)** | `PAUSED_MISSING_INFO` (ask customer); hidden instruction: `ROUTED_TO_HUMAN` |
| S2 | Tool/Code | Look up policy record + retrieve relevant policy clauses | `FAILED` (policy not found) |
| S3 | **Model (LLM)** | Extract date, type, amount, police report, vehicle, summary as JSON | API error: 3 attempts with backoff; then `ROUTED_TO_HUMAN` |
| S4 | Code | Schema validation (Pydantic) + cross-check with form (date, amount ±5%, **police report if the form has it**) | Invalid JSON: 2 re-prompts; mismatch: `ROUTED_TO_HUMAN` |
| S5 | Code (rules) | Coverage: bind date, limit, deductible, police-report rule | `NOT_COVERED`/`AMBIGUOUS` → `ROUTED_TO_HUMAN` |
| S6 | Code | Build fraud features | – |
| S7 | Model (ML) | Logistic-regression fraud score | Fallback to rule-based score |
| S8 | Code | Check score is between 0 and 1, assign LOW / MEDIUM / HIGH | Fallback score is labelled `UNVERIFIED`, never LOW |
| S9 | Model (LLM) | Explain risk indicators for the adjuster | Template explanation |
| S10 | Code | Save adjuster review package and create a pending review | → `COMPLETED_PENDING_ADJUSTER` |
| H1 | Human (simulated) | Exception review: correct fields (re-validated) / confirm coverage / add info, then resume | – |
| **H2** | **Human (real, M3)** | Reviewer approves or rejects the payout; saved pending state, expiry, escalation. **Decisions must be signed (M4 control 2)** | Timeout: escalate, then withhold |
| EXT | External (simulated) | Payment and customer notification services, recorded in SQLite | Check before retry; no duplicate payment |

## Setup

Requires Python 3.10+ and the [Ollama](https://ollama.com/download) desktop app (a recent version; an old one cannot
pull `gpt-oss:20b`). A 16 GB computer is recommended for `gpt-oss:20b`.

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
python test_setup.py             # 1. check LLM connection + dataset
python run_demo.py TC1           # 2. one claim end to end; you act as the reviewer (approve / reject / skip)
python run_m4_tests.py           # 3. Milestone 4: the two controls, blocked + normal cases (real model, a few minutes)
python run_m4_layer2_tests.py    # 4. Milestone 4: police-report check at S4 (stand-in model, seconds, no Ollama)
python run_m3_tests.py           # 5. Milestone 3 tests: review, duplicates, crash, lost confirmation (~5 min)
python cost_model.py 3000        # 6. production cost per claim and per month at 3000 claims/month
python run_tests.py              # 7. Milestone 2 regression: 19 scenarios (~15 min)
```

On macOS / Linux use `python3` instead of `python` where needed.

Milestone 2 scripts, still available: `make_test_claims.py` (builds `data/test_claims.csv`),
`train_risk_model.py` (ML vs rules), `compare_prompts.py` (Prompt A vs B, ~10 min).

Note: the test scripts reset the local database (`db/triage.db`) and rewrite their own result files.

## Milestone 4: controls and tests

Two controls, both plain code that runs outside the model, plus a second layer:

| Control | Where | What it does |
|---|---|---|
| 1 Narrative guard | `src/guard.py`, called in S1 | Stops a claim whose narrative contains an instruction to the AI (override, role markers, dictated output, hidden characters), before any model call; the claim goes to human review (H1) |
| 2 Signed decisions | `src/auth.py`, called first in `handle_event` | Refuses any APPROVE or REJECT event without a valid signature from that reviewer; nothing is stored or paid |
| Second layer | `cross_check` in `src/extraction.py` | If the claim form has `form_police_report` (YES or NO) and the model's answer disagrees, the claim goes to H1 |

What each test checks (and where the result is read):

| Test file | Blocked case | Normal case | Checked in |
|---|---|---|---|
| `run_m4_tests.py` (control 1) | Narrative with a hidden instruction: stopped at S1, 0 prompts sent to the model | TC1 narrative unchanged: reaches the model and creates a pending review | The prompts really sent to the model, the log file, the payment records |
| `run_m4_tests.py` (control 2) | 4 forged approvals (unsigned, wrong key, altered after signing, inbox file): 0 payments | Correctly signed approval by adjuster_1: exactly 1 payment | Events table, payment records, log file |
| `run_m4_layer2_tests.py` | Model obeys a reworded instruction, form says no police report: stopped at S4 | Form and narrative agree: passes to review | Claim state, reviews, payment records, log file |

Evidence files: `results/m4_test_results.csv`, `m4_model_inputs.csv`, `m4_payments.csv`, `m4_events.csv`,
`m4_layer2_results.csv`.

Known limits (see the Milestone 4 report):

- The narrative guard is a pattern list. A reworded instruction passes it (case 0 in `run_m4_layer2_tests.py`).
- The second layer works only when the claim form carries the police-report answer. Without it (case 3), the
  hidden instruction can reach the reviewer. The next step is to make that field required on the intake form.
- The signing keys in `src/auth.py` are made-up TEST-ONLY values in a simple role table (`adjuster_1`,
  `supervisor_1`, `clerk_9`). A real deployment would use a secrets manager or single sign-on.
- `run_m4_layer2_tests.py` replaces the model with a stand-in, so it shows what the workflow does when a model
  obeys a hidden instruction, not whether our model obeys it.

## Try the human checkpoint yourself

```bash
python run_demo.py TC1                                              # creates a pending review
python review_cli.py list                                           # all reviews and their status
python review_cli.py show R-xxxxxx                                  # what the reviewer sees
python review_cli.py decide R-xxxxxx approve --reviewer adjuster_1 --event-id E-1   # writes a signed inbox/E-1.json
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
| `COMPLETED_PENDING_ADJUSTER` | reviewer APPROVE (valid, signed, in time) | `APPROVED_EXECUTING` |
| `COMPLETED_PENDING_ADJUSTER` | reviewer REJECT | `DENIED` |
| `COMPLETED_PENDING_ADJUSTER` | 48 h without decision | `ESCALATED_TO_SUPERVISOR` |
| `ESCALATED_TO_SUPERVISOR` | supervisor APPROVE / REJECT | `APPROVED_EXECUTING` / `DENIED` |
| `ESCALATED_TO_SUPERVISOR` | 24 h more without decision | `WITHHELD_EXPIRED` |
| `APPROVED_EXECUTING` | payment confirmed, or found by lookup | `PAID` |
| `APPROVED_EXECUTING` | 3 failed attempts | `PAYOUT_UNCERTAIN` (recovery retries after checking the payment system) |
| any closed state | repeated, late or stale event | no change (logged) |
| any state | decision event with a missing or wrong signature (M4) | no change; refused and logged |

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
| `results/m4_test_results.csv`, `m4_model_inputs.csv`, `m4_payments.csv`, `m4_events.csv` | Milestone 4 control tests and what they left in the model input and the payment system |
| `results/m4_layer2_results.csv` | Milestone 4 second-layer (police-report check) tests |
| `results/cost_model.csv` | Cost per claim and per month for each deployment option |
| `results/risk_model_comparison.csv` | ML vs rule-based fraud scoring |
| `results/prompt_comparison_summary.csv` | Prompt A vs Prompt B extraction results |

## Project structure

```
review_cli.py          Reviewer / operations commands (list, show, decide, worker, sweep, ledger)
run_demo.py            One claim end to end, then act as the reviewer
run_tests.py           Milestone 2 scenarios (regression)
run_m3_tests.py        Milestone 3 tests
run_m4_tests.py        Milestone 4 control tests (narrative guard, signed decisions)
run_m4_layer2_tests.py Milestone 4 second-layer tests (police-report check at S4)
cost_model.py          Production cost model from measured token counts
docs/                  Step contracts (M2), reports and diagrams (M3, M4)
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
  guard.py        M4 control 1: narrative guard (runs before any model call)
  auth.py         M4 control 2: signed reviewer decisions (test users and made-up test keys)
```

## Team contributions and AI use

Per-member contributions for Milestones 3 and 4, with evidence, are in the reports in `docs/`.

AI use: Claude (Anthropic) was the only AI tool used. It helped draft the code, tests, diagrams and reports. The team
ran all tests and checked the results.

## Data

- Mendeley Data `insurance_claims`, Version 2 (A. Aqqad, 2023), DOI 10.17632/992mh7dk9y.2, CC BY 4.0.
- Kaggle `Vehicle Insurance Claim Fraud Detection` (CC0 1.0) was reviewed but not used.
- Policy documents and claim narratives are **synthetic**, created for this project.
- Personal attributes (sex, education, occupation, hobbies, relationship) are excluded from the fraud model.
- No real credentials are stored in this repository. The signing keys in `src/auth.py` are made-up test values.
  `.env`, `db/` and `inbox/processed/` are not committed.

## Known limitations

- The payment and notification services are simulated (SQLite tables), not real systems.
- The risk model is a small logistic regression on a public dataset; its score is advisory only.
- H1 (exception review) is still simulated through `resume_claim()`; only the H2 payout review is a real checkpoint.
- Test narratives are synthetic and cleaner than real customer text.
- Not tested: two workers running at the same time, a real payment API, real human reviewers, and how the real
  model responds to a reworded hidden instruction.
- Milestone 4 controls are a first layer: see "Known limits" in the Milestone 4 section above.