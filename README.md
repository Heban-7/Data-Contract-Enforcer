# Data Contract Enforcer

Schema Integrity & Lineage Attribution System that auto-generates data contracts from JSONL outputs, validates data against those contracts, traces violations to upstream commits, detects schema evolution, enforces AI-specific contracts, and produces a stakeholder-ready Enforcer Report.

## Prerequisites

- Python 3.11+
- pip

Install dependencies:

```bash
pip install -r requirements.txt
```

## Quick Start — Full Pipeline

Run the following steps in order from the repository root.

### Step 0: Generate Sample Data

If the `outputs/` directory is empty, generate sample data:

```bash
python scripts/generate_sample_data.py
```

Expected output: 55+ records in each JSONL file under `outputs/`.

### Step 1: Generate Contracts

Generate contracts for Week 3 extractions and Week 5 events:

```bash
python contracts/generator.py \
  --source outputs/week3/extractions.jsonl \
  --contract-id week3-document-refinery-extractions \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --output generated_contracts/

python contracts/generator.py \
  --source outputs/week5/events.jsonl \
  --contract-id week5-event-records \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --output generated_contracts/
```

Expected output:
- `generated_contracts/week3_document_refinery_extractions.yaml` (17 schema clauses)
- `generated_contracts/week3_document_refinery_extractions_dbt.yml`
- `generated_contracts/week5_event_records.yaml` (14 schema clauses)
- `generated_contracts/week5_event_records_dbt.yml`
- Timestamped snapshots in `schema_snapshots/`

### Step 2: Run Validation (Clean Data)

```bash
python contracts/runner.py \
  --contract generated_contracts/week3_document_refinery_extractions.yaml \
  --data outputs/week3/extractions.jsonl \
  --output validation_reports/clean_run.json
```

Expected output: `validation_reports/clean_run.json` — all structural checks PASS. Baselines written to `schema_snapshots/baselines.json`.

### Step 3: Inject Violations

```bash
python create_violation.py
```

Expected output:
- `outputs/week3/extractions_violated.jsonl` (confidence scaled to 0-100)
- `outputs/week3/extractions_enum_violated.jsonl` (invalid entity type)

### Step 4: Run Validation (Violated Data)

```bash
python contracts/runner.py \
  --contract generated_contracts/week3_document_refinery_extractions.yaml \
  --data outputs/week3/extractions_violated.jsonl \
  --output validation_reports/violated_run.json

python contracts/runner.py \
  --contract generated_contracts/week3_document_refinery_extractions.yaml \
  --data outputs/week3/extractions_enum_violated.jsonl \
  --output validation_reports/enum_violated_run.json
```

Expected output:
- `violated_run.json`: 2 FAIL results (confidence range + statistical drift)
- `enum_violated_run.json`: 1 FAIL result (entity type enum violation)

### Step 5: Attribute Violations

```bash
python contracts/attributor.py \
  --violation validation_reports/violated_run.json \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --contract generated_contracts/week3_document_refinery_extractions.yaml \
  --output violation_log/violations.jsonl

python contracts/attributor.py \
  --violation validation_reports/enum_violated_run.json \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --contract generated_contracts/week3_document_refinery_extractions.yaml \
  --output violation_log/violations.jsonl
```

Expected output: `violation_log/violations.jsonl` with 3 violation records, each containing a blame chain with ranked candidates and blast radius.

### Step 6: Schema Evolution Analysis

First, generate a second contract snapshot from the violated data to create a diff:

```bash
python contracts/generator.py \
  --source outputs/week3/extractions_violated.jsonl \
  --contract-id week3-document-refinery-extractions \
  --lineage outputs/week4/lineage_snapshots.jsonl \
  --output generated_contracts/
```

Then run the analyzer:

```bash
python contracts/schema_analyzer.py \
  --contract-id week3-document-refinery-extractions \
  --output validation_reports/schema_evolution_week3.json
```

Expected output: `validation_reports/schema_evolution_week3.json` with a compatibility verdict, list of changes, migration checklist, and rollback plan.

### Step 7: AI Contract Extensions

```bash
python contracts/ai_extensions.py \
  --mode all \
  --extractions outputs/week3/extractions.jsonl \
  --verdicts outputs/week2/verdicts.jsonl \
  --output validation_reports/ai_extensions.json
```

Expected output:
- `validation_reports/ai_extensions.json` with embedding drift score, prompt validation results, and LLM output violation rate
- `validation_reports/ai_metrics.json` with summary metrics

### Step 8: Generate Enforcer Report

```bash
python contracts/report_generator.py
```

Expected output: `enforcer_report/report_data.json` with:
- `data_health_score` between 0 and 100
- Plain-language violation descriptions
- Schema change summary
- AI system risk assessment
- 3 prioritized recommendations

## Repository Structure

```
Data-Contract-Enforcer/
├── contracts/
│   ├── generator.py           # ContractGenerator entry point
│   ├── runner.py              # ValidationRunner entry point
│   ├── attributor.py          # ViolationAttributor entry point
│   ├── schema_analyzer.py     # SchemaEvolutionAnalyzer entry point
│   ├── ai_extensions.py       # AI Contract Extensions entry point
│   └── report_generator.py    # EnforcerReport entry point
├── generated_contracts/       # Auto-generated YAML contract files
├── validation_reports/        # Structured validation report JSON
├── violation_log/             # Violation records JSONL
├── schema_snapshots/          # Timestamped schema snapshots
├── enforcer_report/           # Stakeholder report data
├── outputs/                   # Input data (JSONL from Weeks 1-5)
├── scripts/
│   └── generate_sample_data.py
├── create_violation.py        # Violation injection script
├── DOMAIN_NOTES.md            # Phase 0 domain analysis
├── requirements.txt
└── README.md
```

## Entry Points

| Script | Purpose | Key Input | Key Output |
|--------|---------|-----------|------------|
| `contracts/generator.py` | Generate contracts from data | JSONL + lineage | YAML contracts + dbt schema |
| `contracts/runner.py` | Validate data against contracts | Contract YAML + JSONL | Validation report JSON |
| `contracts/attributor.py` | Trace violations to commits | Validation report + lineage | Blame chain JSONL |
| `contracts/schema_analyzer.py` | Classify schema changes | Schema snapshots | Evolution report JSON |
| `contracts/ai_extensions.py` | AI-specific contract checks | Extractions + verdicts | AI metrics JSON |
| `contracts/report_generator.py` | Generate stakeholder report | All outputs | `report_data.json` |
