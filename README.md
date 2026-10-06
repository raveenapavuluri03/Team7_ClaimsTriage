# Team 7 – Insurance Claims Intake Triage (MSA 8770, Milestone 2)

Prototype workflow that turns a submitted insurance claim (form fields + free-text narrative) into a
validated adjuster review package. The final approve/deny decision always stays with a human adjuster.

**Team:** Raveena Pavuluri, Nikhithareddy Obili, Ishita Varma Kalindindi, Tejaswini Waghmare, Kiarash Rezaei

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
| S8 | Code | Check score is between 0 and 1, assign LOW / MEDIUM / HIGH | Fallback, then `ROUTED_TO_HUMAN` |
| S9 | Model (LLM) | Explain risk indicators for the adjuster | Template explanation |
| S10 | Code | Save adjuster review package | → `COMPLETED_PENDING_ADJUSTER` |
| H1 | Human (simulated) | Exception review: correct fields / confirm coverage / add info, then resume | – |
| H2 | Human (simulated) | Adjuster approves or denies; payment & notification are mocked | – |

## Setup

Requires Python 3.10+ and [Ollama](https://ollama.com/download).

**1. Install Ollama and get a model** (choose one)

```bash
ollama signin                 # option A: Ollama cloud model through the local app
ollama pull llama3.2:3b       # option B: fully local model
```

**2. Install the project**

Windows (Command Prompt):
```cmd
git clone <repo-url>
cd team7-claims-triage
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

macOS / Linux:
```bash
git clone <repo-url>
cd team7-claims-triage
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `OLLAMA_MODEL` to the model you pulled (e.g. `gpt-oss:20b-cloud` or `llama3.2:3b`).

## Run (in this order)

```bash
python test_setup.py           # 1. check LLM connection + dataset
python make_test_claims.py     # 2. build 12 test claims -> data/test_claims.csv
python train_risk_model.py     # 3. train fraud model, compare ML vs rules -> results/
python run_demo.py TC1         # 4. one claim end to end (try TC2, TC4 for other branches)
python run_tests.py            # 5. all tests + failure paths + resume + decision (~15 min)
python compare_prompts.py      # 6. Prompt A vs B on the same 10 narratives (~10 min)
```

## Outputs (execution evidence)

| File | Contents |
|---|---|
| `logs/run_log.jsonl` | One line per step: timestamp, run id, claim, step, status, attempt, latency, detail |
| `states/<claim>.json` | Full workflow state: inputs, results per step, errors, retry counts, human actions, metrics |
| `outputs/<claim>_package.json` | Adjuster review package |
| `results/test_results.csv` | Expected vs actual for every test scenario |
| `results/risk_model_comparison.csv` | ML vs rule-based fraud scoring |
| `results/prompt_comparison_summary.csv` | Prompt A vs Prompt B extraction results |

## Project structure

```
src/
  llm.py          LLM client (Ollama) + latency/token metrics
  data_prep.py    Load and clean the Mendeley dataset
  policies.py     Synthetic policies, policy retrieval (S2), coverage rules (S5)
  extraction.py   Prompts, LLM output validation and cross-check (S3-S4)
  risk.py         Fraud features, ML score, rule-based fallback (S6-S8)
  state.py        Workflow state, persistence, logging
  pipeline.py     Orchestrator: sequence, branches, retries, fallbacks, resume, human steps
```

## Data

- Mendeley Data `insurance_claims`, Version 2 (A. Aqqad, 2023), DOI 10.17632/992mh7dk9y.2, CC BY 4.0.
- Policy documents and claim narratives are **synthetic**, created for this project.
- Personal attributes (sex, education, occupation, hobbies, relationship) are excluded from the fraud model.
- No API keys are stored in this repository.