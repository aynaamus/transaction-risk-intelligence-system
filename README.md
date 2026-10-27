# Transaction Risk Intelligence System (TRIS)

A local, SQL-driven transaction-monitoring and investigation workbench built on
**synthetic** data: DuckDB ingestion, three explainable detection rules, risk
scoring, account-network exploration, held-out evaluation, and a Streamlit
investigator application with a persistent, audited case workflow.

> **All data is synthetic.** TRIS runs on the PaySim simulator output. Alerts are
> suspicious-activity indicators, not confirmed fraud and not evidence of money
> laundering. `isFraud` is a simulated label, not a real-world finding.

## Architecture

```
PaySim CSV (read-only) --ingest--> DuckDB: transactions, ingestion_log
                                        |
                                   detect (SQL) --> rule_thresholds, alert_evidence, alerts
                                        |
        investigation_cases, case_notes, case_audit  (separate tables, survive re-detection)
                                        |
            Streamlit app  <--  tris.alerts / tris.cases / tris.network / tris.evaluate
```

| Module | Role |
|---|---|
| `tris.ingest`, `tris.validate` | Idempotent, transactional CSV load (SHA-256 matched) and SQL validation |
| `tris.detect` | Rules A/B/C, scoring, severity (SQL in DuckDB; nothing is loaded into Python memory) |
| `tris.evaluate` | Confusion matrices vs `isFraud`, **evaluation only** |
| `tris.network` | Bounded NetworkX neighbourhoods, Plotly figure, HTML/GraphML export |
| `tris.alerts` | Database-side filtering, sorting and pagination; alert detail and evidence |
| `tris.cases` | One case per alert, notes, append-only audit trail |
| `tris.app` | Streamlit UI; renders the modules above and contains no business logic |

One DuckDB file (`data/tris.duckdb`) holds everything. No other services.

## Dataset provenance

[PaySim](https://www.kaggle.com/datasets/ealaxi/paysim1) is a mobile-money
simulator (E. Lopez-Rojas, A. Elmir, S. Axelsson, 2016) calibrated on aggregated
real logs. The file used here, `PS_20174392719_1491204439457_log.csv`, has
6,362,620 rows, 8,213 of them labelled `isFraud = 1`, and 16 with
`isFlaggedFraud = 1`. The CSV is not redistributed and is git-ignored; download it
yourself.

Data-use rules enforced in code (`tris.schema.FEATURE_EXCLUDED_COLUMNS`):
- `isFraud` is simulated ground truth, used **only** in `tris.evaluate`.
- `isFlaggedFraud` is the simulator's own rule output, not ground truth. Never used.
- `oldbalanceOrg`, `newbalanceOrig`, `oldbalanceDest`, `newbalanceDest` are stored for
  fidelity but excluded from detection (leakage risk per the dataset authors).
- `step` is simulated hours, not a timestamp.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```
Create `data/raw/` (`mkdir -p data/raw`) and place the CSV at `data/raw/PS_20174392719_1491204439457_log.csv`.

## Reproducible commands

```bash
.venv/bin/python -m tris.cli ingest              # load + validate; rerun is a no-op (use --replace to rebuild)
.venv/bin/python -m tris.cli detect              # rebuild thresholds, evidence, alerts (idempotent, ~2 s)
.venv/bin/python -m tris.cli evaluate            # held-out confusion matrices
.venv/bin/python -m tris.cli network --row-id 5959198   # exports to data/exports/ (git-ignored)
.venv/bin/python -m pytest                       # full test suite
.venv/bin/streamlit run src/tris/app.py          # investigator application
```
DuckDB allows one writer process: close the app before running `detect` or `ingest`.
Re-running `detect` never touches cases, notes or audit history (they are keyed by the
source `row_id`).

## Detection methods

Inputs are only `row_id, step, type, amount, nameOrig, nameDest`. Data-dependent
thresholds come from the **reference period** (steps 1-400) and nothing else.

| Rule | Trigger | Weight |
|---|---|---|
| A_LARGE_AMOUNT | amount > per-type p99 of reference amounts (CASH_IN 552,218; CASH_OUT 580,201; DEBIT 51,714; PAYMENT 59,371; TRANSFER 10,000,000). Zero amounts never alert. | 30 |
| B_DEST_CONCENTRATION | > 21 inbound transactions to one destination within 6 simulated hours (reference p99.9) | 20 |
| C_RAPID_ONWARD | an account receives X then sends 80-100% of X within 6 simulated hours; linked only by a shared account id | 40 |

Score = sum of triggered weights (cap 100). HIGH >= 60, MEDIUM >= 40, otherwise LOW.
Current result: **66,133 alerts** (LOW 65,992, MEDIUM 141, **HIGH 0**). Each alert stores
the rule, threshold, observed value, explanatory text and any linked transaction `row_id`.

## Evaluation methodology and results

Chronological split on simulated steps: thresholds from steps 1-400, evaluation on the
held-out steps 401-743 (575,590 transactions, 3,736 simulated-fraud, prevalence 0.65%).
Nothing was tuned against held-out labels.

| System | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| Combined | 837 | 4,269 | 2,899 | 0.164 | 0.224 | 0.189 |
| Rule A | 837 | 4,268 | 2,899 | 0.164 | 0.224 | 0.189 |
| Rule B | 0 | 0 | 3,736 | undefined | 0.000 | undefined |
| Rule C | 0 | 1 | 3,736 | 0.000 | 0.000 | undefined |

## Why performance is limited

- Detection is effectively Rule A (amount outliers); it catches 22% of held-out simulated fraud.
- Rule B's 5,658 alerts all fall in steps 1-99, a simulator start-up
  artifact. It fires nothing in the held-out period.
- Rule C has little to work with: nearly every origin account appears once (max 3
  transactions), so only 332 inbound-then-outbound pairs exist within 6 hours; 16 meet the
  amount criterion and one falls in the held-out period (a false positive).
- PaySim fraud is generated by simulator scripts, and the TRANSFER amount threshold equals the
  simulator's 10,000,000 cap, so only amounts above the cap alert for that type. The balance
  columns are excluded on purpose (leakage risk), which removes information a classifier
  might otherwise use.
- Therefore the figures say little about real-world fraud or AML detection.

## Investigator application

Five views: **Overview** (monitoring statistics, kept separate from evaluation),
**Alert Queue** (server-side pagination, sorting and filtering by rule, severity, step,
type, status; only one page is ever loaded), **Alert Investigation** (transaction, score,
per-rule evidence with thresholds, linked transaction IDs, case workflow),
**Account Network** (bounded neighbourhood, directed edges, counts, amounts, steps,
source transaction IDs; rendered with Streamlit's bundled plotly.js, no CDN), and
**Detection Performance** (the table above, with the limitations stated in-app).

Case workflow: statuses `OPEN -> IN_REVIEW -> ESCALATED -> CLOSED`, dispositions
(`NO_FURTHER_ACTION`, `REFER_FOR_REVIEW`, `INCONCLUSIVE`), analyst finding and notes. One case
per alert; closing requires a disposition. Every change is written to the append-only
`case_audit` table (timestamp, analyst, field, old and new value) in the same transaction.
Alert prioritisation never reads the ground-truth label. The "Analyst ID" is a local label,
not authentication; the app is single-user and local.

## Example investigation (existing data, no conclusion drawn)

Transaction `5959198`, alert `C_RAPID_ONWARD`, score 40 (MEDIUM). Reproduce with
`streamlit run src/tris/app.py`, then Alert Investigation -> 5959198, or
`python -m tris.cli network --row-id 5959198`.

- Rule evidence: account `C1190668648` received 301,163.17 (transaction `5732961`, step 399)
  and sent 277,317.94 (transaction `5959198`, step 405) six simulated hours later: 92.1% of
  the inflow, inside the 80-100% / 6-hour criteria.
- This establishes only a timing and amount match between two transactions sharing an
  account id. It does not show intent, ownership, control or laundering. Both legs are
  `CASH_IN` rows, so the pattern may simply reflect how the simulator generates activity.
- No finding is recorded for this alert in the repository; an analyst would record theirs
  through the case workflow.

## Limitations

Synthetic data; few observable multi-hop chains; weak held-out performance; fixed
reference/held-out split; rule parameters are defensible defaults, not optimised;
single-user local application with no authentication; audit append-only is enforced by the
application's API, not by database permissions.

## Repository hygiene

`.gitignore` excludes raw data (`data/raw/`, `*.csv`), databases (`*.duckdb`), exports
(`data/exports/`), virtual environments, credentials, and temporary files. The database
records the absolute path of the ingested CSV in `ingestion_log`, another reason not to
publish it. Nothing has been deployed or pushed.
