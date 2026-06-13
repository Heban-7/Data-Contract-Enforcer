# Data Contract Enforcer — Final Submission Report

**Project:** TRP1 Challenge Week 7 — Data Contract Enforcer  
**Repository:** `Data-Contract-Enforcer`  
**Report Date:** 2026-04-04  
**Prepared by:** Data Contract Enforcer Team

---

## 1) Auto-generated Enforcer Report

This report section is based on machine-generated outputs from the live pipeline, not hand-written summaries.

### 1.1 Data Health Score (with formula)

- **Data Health Score:** **50 / 100**
- **Narrative:** Data quality is degraded, with 2 critical violations requiring immediate intervention.
- **Computation source:** `enforcer_report/report_data.json`

**Formula used by the generated report implementation (`contracts/report_generator.py`):**

`score = 100 - (20 * CRITICAL) - (10 * HIGH) - (5 * MEDIUM) - (1 * LOW)`

**Calculation for this report window:**

- CRITICAL = 2 -> 2 * 20 = 40
- HIGH = 1 -> 1 * 10 = 10
- MEDIUM = 0 -> 0
- LOW = 0 -> 0

`score = 100 - 40 - 10 = 50`

### 1.2 Violations This Week

- **Total violations (in report window):** 3
- **By severity:** CRITICAL=2, HIGH=1, MEDIUM=0, LOW=0
- **Top violations:**
  - `entities_type` enum violation (`INSTITUTION` observed, not allowed by contract enum)
  - `extracted_facts_confidence` range violation (observed max=98.0 vs expected max<=1.0)
  - `extracted_facts_confidence` statistical drift violation (z-score=612.92)

### 1.3 Schema Changes Detected

- **Contract analyzed:** `week3-document-refinery-extractions`
- **Verdict:** **BREAKING**
- **Detected breaking change:** `extracted_facts_confidence` changed from number `[0.0, 1.0]` to integer `[0, 100]`
- **Source:** `validation_reports/schema_evolution_week3.json`

### 1.4 AI System Risk Assessment

- **LLM output schema status:** PASS
- **Violation rate:** 0.0
- **Trend:** stable
- **Source:** `validation_reports/ai_extensions.json`, `validation_reports/ai_metrics.json`

### 1.5 Recommended Actions (fully actionable)

1. **Fix confidence scale at producer source**
   - Update producer file: `src/extractor.py` (Week 3 source system)
   - Contract clause to satisfy: `generated_contracts/week3_document_refinery_extractions.yaml` -> `schema.extracted_facts_confidence` (`minimum: 0.0`, `maximum: 1.0`)
   - Validation checks to clear: `week3-document-refinery-extractions.extracted_facts_confidence.range` and `week3-document-refinery-extractions.extracted_facts_confidence.statistical_drift`
2. **Repair enum contract and validation path**
   - Update contract file: `generated_contracts/week3_document_refinery_extractions.yaml`
   - Clause: `schema.entities_type.enum` (include canonical set used by downstream consumers)
   - Re-generate dbt artifact: `generated_contracts/week3_document_refinery_extractions_dbt.yml`
   - Validation check to clear: `week3-document-refinery-extractions.entities_type.enum`
3. **Pin release gate policy in CI**
   - CI command: `python contracts/runner.py --contract generated_contracts/week3_document_refinery_extractions.yaml --data <snapshot> --mode ENFORCE`
   - This blocks publish on any FAIL/ERROR for the Week 3 contract path and prevents confidence regressions from reaching Week 4 consumers.

---

## 2) Validation Run Results

### 2.1 Clean Baseline Run

- **Report:** `validation_reports/week3_clean_run.json`
- **Total checks:** 37
- **Passed:** 37
- **Failed:** 0
- **Warned:** 0
- **Errored:** 0
- **Conclusion:** Baseline run passed all checks and established initial numeric quality profile.

### 2.2 Violated Enforce Run

- **Report:** `validation_reports/violated_run_enforce.json`
- **Mode:** ENFORCE
- **Blocking:** true
- **Total checks:** 42 (37 contract-clause checks + 5 drift checks)
- **Passed:** 40
- **Failed:** 2
- **Warned:** 0
- **Errored:** 0

### 2.3 Independent Detection Confirmation

The confidence scale mutation (0.0-1.0 -> 0-100) triggered **two independent paths**:

1. `contract_range` check fail on `extracted_facts_confidence.range` (CRITICAL)
2. `statistical_drift` check fail on `extracted_facts_confidence.statistical_drift` (HIGH)

### 2.4 Downstream Consumer Consequence (explicit)

- **Named downstream consumer:** `week4` (from `contracts/subscriptions_registry.json`, subscription `sub-week3-to-week4-extraction-lineage`)
- **Field consumed:** `extracted_facts_confidence`
- **Specific consequence:** Week 4 lineage quality interpretation ingests confidence as if normalized in `[0.0,1.0]`; when producer emits `0-100`, confidence semantics are inflated and downstream lineage-derived confidence interpretation becomes unreliable.
- **Why this matters beyond blocking:** this is a semantic contamination risk that can propagate even when data remains syntactically valid JSON.

---

## 3) Violation Deep-Dive: Blame Chain and Blast Radius

### 3.1 Violation Selected

- **Check ID:** `week3-document-refinery-extractions.extracted_facts_confidence.range`
- **Severity:** CRITICAL
- **Failure detail:** observed confidence values in `55.0-98.0` instead of expected `0.0-1.0`
- **Records failing:** 251

### 3.2 Validation Evidence

- **Validation mode:** ENFORCE
- **Blocking result:** `true`
- **Blocking reason:** ENFORCE blocks on any FAIL/ERROR
- **Report path:** `validation_reports/violated_run_enforce.json`

### 3.3 Lineage Traversal and Attribution

- ViolationAttributor loaded:
  - `outputs/week4/lineage_snapshots.jsonl`
  - `contracts/subscriptions_registry.json`
- Upstream candidates were ranked via git recency + lineage distance scoring.
- Top blame candidate observed:
  - `commit_hash`: `f0617cdae1841d22bb28db926e4f021526380809`
  - `author`: `liuljima1896@gmail.com`
  - `commit_message`: `data contractor`
  - `confidence_score`: `0.8`

**Step-by-step lineage path used for attribution context (from `outputs/week4/lineage_snapshots.jsonl` + registry):**

1. `file::src/extractor.py` -> `table::extractions` (`WRITES`)
2. `table::extractions` -> `service::week3-refinery` (`CONSUMES`)
3. `service::week3-refinery` -> `pipeline::extraction-pipeline` (`PRODUCES`)
4. `pipeline::extraction-pipeline` -> `external::langsmith` (`WRITES`)
5. Subscription mapping for contract impact: producer `week3` -> direct subscriber `week4` (`sub-week3-to-week4-extraction-lineage`)

### 3.4 Blast Radius

- **Affected node(s):** `week4`
- **Affected pipelines:** none listed for this violation
- **Contamination depth:** 1
- **Depth source:** lineage traversal/subscription graph linkage
- **Subscription link used:** `sub-week3-to-week4-extraction-lineage`

**Direct vs transitive contamination distinction:**

- **Direct subscribers (depth=1):** `week4`
- **Transitively contaminated nodes (depth>=2 via subscriptions graph):** `week7_attributor` (through `week4 -> week7_attributor`)

---

## 4) Schema Evolution Case Study

### 4.1 Change Summary

- **Old snapshot:** `20260401_192140.yaml`
- **New snapshot:** `20260401_193246.yaml`
- **Contract:** `week3-document-refinery-extractions`
- **Verdict:** BREAKING

### 4.2 Breaking Change Details

- Field: `extracted_facts_confidence`
- Change type: `type_narrowing, range_change`
- From:
  - `type: number`
  - `minimum: 0.0`
  - `maximum: 1.0`
- To:
  - `type: integer`
  - `minimum: 0`
  - `maximum: 100`

### 4.3 Compatibility and Migration Impact

- **Compatibility:** Not backward compatible.
- **Expected downstream failure mode:** consumers expecting normalized confidence in `[0.0,1.0]` silently ingest inflated confidence values.
- **Migration checklist (two concrete steps):**
  1. Update Week 3 producer implementation to emit confidence as float in `[0.0,1.0]` and rerun `contracts/runner.py` in ENFORCE mode until both `range` and `statistical_drift` checks pass.
  2. Run Week 4 consumer validation with regenerated Week 3 outputs and verify confidence-dependent downstream logic remains semantically correct.
- **Rollback plan (with baseline re-establishment):**
  - Revert `extracted_facts_confidence` clause to `type:number`, `minimum:0.0`, `maximum:1.0`.
  - Re-establish numeric baselines in `schema_snapshots/baselines.json` for at least:
    - `extracted_facts_confidence`
    - `processing_time_ms`
    - `token_count_input`
    - `token_count_output`
    - `extracted_facts_page_ref`
- **Production-tool comparison:**
  - A production schema registry (e.g., compatibility-checked schema evolution workflow) would flag this as incompatible for existing consumers.
  - A production data-quality stack would require both structural constraints and distribution checks; this project mirrors that by combining explicit range clauses with independent drift detection.
- **Source:** `validation_reports/schema_evolution_week3.json`

---

## 5) AI Contract Extension Results

### 5.1 Extension 1 — Embedding Drift Detection

- **Method:** centroid-based cosine distance between baseline and current embedding samples
- **Sample size:** 251 extracted fact texts
- **Drift score:** 0.0
- **Threshold:** 0.15
- **Status:** PASS (`0.0 < 0.15`)

### 5.2 Extension 2 — Prompt Input Schema Validation

- **Total inputs validated:** 60
- **Valid:** 60
- **Quarantined:** 0
- **Quarantine rate:** 0.0
- **Status:** PASS

### 5.3 Extension 3 — Structured LLM Output Enforcement

- **Status:** PASS
- **Total outputs evaluated:** 55
- **Schema violations:** 0
- **Violation rate:** 0.0
- **Trend:** stable
- **Warn threshold:** 0.02

### 5.4 Interpretation

- All three AI extensions executed on this run and returned PASS.
- No quarantine events and no schema violations indicate stable AI data contracts for this reporting window.

---

## 6) Highest-Risk Interface Analysis

### Highest-Risk Interface

**Week 3 -> Week 4** on `extracted_facts_confidence`.

### Why this is highest risk

1. It is semantically critical and consumed downstream for lineage-derived quality interpretation.
2. It can fail **silently** if scale/type drift occurs while structural parsing still succeeds.
3. It has already demonstrated both:
   - contract range failure (CRITICAL), and
   - statistical drift failure (HIGH),
   from a single real-world-like mutation (0.0-1.0 -> 0-100).
4. Blast radius evidence shows immediate contamination of downstream contract consumers.

### Preventive Control

- Keep dual-path detection active:
  - contract range checks (`contract_range`)
  - statistical drift checks (`statistical_drift`)
- Run ValidationRunner in `ENFORCE` mode for release gating.

### Enforcement Gap and New Mitigation

**Current gap:**

- Existing checks can catch confidence scale drift when range and drift checks are present.
- However, purely structural checks (required/type/pattern) can miss semantically wrong numeric scale changes.

**New concrete mitigation (additional control):**

- Add a dedicated interface semantic guard in `contracts/runner.py`:
  - fail if `p95(extracted_facts_confidence) > 1.0`
  - fail if `mean(extracted_facts_confidence) / baseline_mean > 3.0`
- Enforce this guard in CI before publishing Week 3 outputs to Week 4 consumers.

---

## 7) Contract Registry Coverage (Criterion Evidence)

The subscriptions registry is implemented at:

- `contracts/subscriptions_registry.json`

It includes the required four-plus producer->consumer subscriptions with:

- `contract_id`
- `subscriber_id`
- `fields_consumed`
- `breaking_fields` with reasons
- `validation_mode`
- `registered_at`
- `contact`

Required coverage mapping is explicitly declared in `required_coverage`:

- week1 -> week2
- week3 -> week4
- week4 -> week7_attributor
- week5 -> week7_runner

---

## 8) Reproducibility Appendix (Commands)

```bash
python contracts/generator.py --source outputs/week3/extractions.jsonl --contract-id week3-document-refinery-extractions --lineage outputs/week4/lineage_snapshots.jsonl --output generated_contracts
python contracts/runner.py --contract generated_contracts/week3_document_refinery_extractions.yaml --data outputs/week3/extractions_violated.jsonl --output validation_reports/violated_run_enforce.json --mode ENFORCE
python contracts/attributor.py --violation validation_reports/violated_run_enforce.json --lineage outputs/week4/lineage_snapshots.jsonl --contract generated_contracts/week3_document_refinery_extractions.yaml --subscriptions contracts/subscriptions_registry.json --output violation_log/violations.jsonl
python contracts/schema_analyzer.py --contract-id week3-document-refinery-extractions --output validation_reports/schema_evolution_week3.json
python contracts/ai_extensions.py --mode all --extractions outputs/week3/extractions.jsonl --verdicts outputs/week2/verdicts.jsonl --output validation_reports/ai_extensions.json
python contracts/report_generator.py
```

---

## 9) PDF Export Note

This markdown file is formatted to be directly exported to PDF from Cursor/VS Code or any Markdown-to-PDF tool.  
Recommended output filename: `report_final_2026-04-04.pdf`.

