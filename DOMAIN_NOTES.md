# Domain Notes — Data Contract Enforcer

## Question 1: Backward-Compatible vs Breaking Schema Changes

A **backward-compatible** change allows existing consumers to continue reading data without modification. A **breaking** change forces downstream consumers to update their code or fail.

### Three Backward-Compatible Examples

1. **Adding a nullable `notes` field to Week 1 `intent_record`.**
   The existing schema has `intent_id`, `description`, `code_refs`, `governance_tags`, `created_at`. Adding `"notes": "string | null"` is safe because every downstream consumer (Week 2 Courtroom uses `code_refs[].file` as `target_ref`) simply ignores the new field. No existing parsing logic references `notes`, so no consumer breaks.

2. **Adding a new enum value `"CONCEPT"` to Week 3 `entities[].type`.**
   The current enum is `PERSON | ORG | LOCATION | DATE | AMOUNT | OTHER`. Adding `CONCEPT` is additive. The Week 4 Cartographer ingests entities as node metadata and stores the type as a string label. A new value passes through without error. Consumers that switch on entity type will fall through to a default branch (or to `OTHER` handling), which is the expected behavior for unknown types.

3. **Widening `processing_time_ms` from `int32` to `int64` in Week 3 `extraction_record`.**
   The field stores milliseconds. Current values range from 400 to 3500. Widening to int64 preserves all existing values exactly. Any consumer reading this as a numeric type in pandas will see `int64` regardless, since pandas defaults to 64-bit integers. No precision loss, no behavioral change.

### Three Breaking Examples

1. **Renaming `confidence` to `confidence_score` in Week 3 `extracted_facts[]`.**
   The Week 4 Cartographer reads `extracted_facts[].confidence` to weight fact nodes in the lineage graph. After the rename, `record['extracted_facts'][0]['confidence']` raises `KeyError`. The pipeline does not crash visibly — it raises an exception inside a list comprehension that may be caught by a broad `except` clause, producing an empty list of weighted facts. The lineage graph loses all fact-level confidence data silently.

2. **Changing `overall_verdict` from enum `PASS | FAIL | WARN` to a numeric score `0.0–1.0` in Week 2 `verdict_record`.**
   Every consumer that checks `if verdict['overall_verdict'] == 'PASS'` will now compare a float to a string, which always evaluates to `False`. All verdicts appear to fail. Downstream reporting shows a 100% failure rate, triggering false alerts.

3. **Removing the `metadata.causation_id` field from Week 5 `event_record`.**
   The event sourcing platform uses `causation_id` to build causal chains between events. Removing it breaks the `build_causal_graph()` function with a `KeyError`. More critically, any event replay logic that depends on causation chains will silently produce incomplete graphs, making it impossible to trace the origin of downstream state changes.

---

## Question 2: Confidence Scale Change — Failure Trace

### Current State (measured from actual data)

Running the profiling script on `outputs/week3/extractions.jsonl`:

```
min=0.550 max=0.980 mean=0.768 stdev=0.124 count=251
```

All confidence values are in the 0.0–1.0 range. The distribution is roughly uniform between 0.55 and 0.98.

### The Breaking Change

An update to `src/week3/extractor.py` changes the confidence output from `0.0–1.0` float to `0–100` integer percentage. After the change:

```
min=55.0 max=98.0 mean=76.8 stdev=12.4 count=251
```

### Propagation Through Week 4 Cartographer

1. The Cartographer reads `outputs/week3/extractions.jsonl` and ingests each `extracted_facts[].confidence` value as edge weight metadata in the lineage graph.
2. The Cartographer's `build_weighted_graph()` function normalizes confidence values assuming they are in `[0.0, 1.0]`. A value of `76.8` is treated as `76.8 / 1.0 = 76.8`, which exceeds any expected threshold.
3. Edge weights above `1.0` cause the graph layout algorithm to distort — high-confidence edges dominate the visualization, and any filtering logic like `if confidence > 0.9` now matches every single fact (since 55.0 > 0.9).
4. The lineage graph becomes meaningless: every fact appears maximally confident, destroying the ability to distinguish reliable extractions from uncertain ones.
5. Downstream consumers of the lineage graph (Week 7 ViolationAttributor, Week 8 Sentinel) inherit the corrupted confidence scores, producing unreliable blame chains and alert thresholds.

### Contract Clause That Catches This

```yaml
# Bitol YAML contract clause for extracted_facts.confidence
schema:
  extracted_facts:
    type: array
    items:
      confidence:
        type: number
        minimum: 0.0
        maximum: 1.0
        required: true
        description: >
          Extraction confidence score. MUST be a float in [0.0, 1.0].
          BREAKING CHANGE if converted to percentage scale (0-100).
          Downstream consumers (Week 4 Cartographer, Week 7 Attributor)
          normalize against this range.
quality:
  type: SodaChecks
  specification:
    checks for extractions:
      - min(extracted_facts_confidence) >= 0.0
      - max(extracted_facts_confidence) <= 1.0
      - avg(extracted_facts_confidence) between 0.3 and 0.99
```

The `maximum: 1.0` structural clause catches any value above 1.0 immediately. The statistical check `avg between 0.3 and 0.99` catches the mean shift from 0.768 to 76.8 — a drift of over 600 standard deviations from the baseline.

---

## Question 3: Blame Chain Construction Using the Lineage Graph

When the ValidationRunner detects a contract violation (e.g., `extracted_facts.confidence.range` FAIL with `max=98.0`), the ViolationAttributor constructs a blame chain through the following steps:

### Step 1: Identify the Failing Schema Element

The violation report contains `check_id: "week3.extracted_facts.confidence.range"`. Parse this to extract the system (`week3`), the table (`extractions`), and the column (`extracted_facts.confidence`).

### Step 2: Load the Lineage Graph

Open the latest snapshot from `outputs/week4/lineage_snapshots.jsonl`. The snapshot contains `nodes[]` (files, tables, services) and `edges[]` (relationships between them).

### Step 3: Locate the Producing Node

Search `nodes[]` for entries where `node_id` contains `week3` and `type == "FILE"`. In our lineage graph, this yields candidates like `file::src/extractor.py`, `file::src/models/entity.py`, etc.

### Step 4: Breadth-First Traversal Upstream

Starting from the failing column's table node (`table::extractions`), traverse edges in reverse (follow `target -> source` direction) using BFS:

```
table::extractions
  <- (WRITES) file::src/extractor.py     [distance=1]
  <- (IMPORTS) file::src/utils.py         [distance=2]
  <- (IMPORTS) file::src/config.py        [distance=3]
```

Stop at the first external boundary or when distance exceeds 5 hops. Collect all FILE-type nodes encountered.

### Step 5: Git Blame on Upstream Files

For each upstream file, run:

```
git log --follow --since="14 days ago" --format='%H|%ae|%ai|%s' -- src/extractor.py
```

This returns recent commits that modified the file. For line-level precision:

```
git blame -L 42,67 --porcelain src/extractor.py
```

### Step 6: Score and Rank Candidates

For each commit, compute a confidence score:

```
score = max(0.0, 1.0 - (days_since_commit * 0.1) - (lineage_distance * 0.2))
```

A commit from 2 days ago at distance 1 scores `1.0 - 0.2 - 0.2 = 0.6`. A commit from 10 days ago at distance 3 scores `1.0 - 1.0 - 0.6 = 0.0` (clamped to 0.0). Rank by score descending, return top 5.

### Step 7: Compute Blast Radius

From the contract YAML's `lineage.downstream[]` section, enumerate all consumers:

```json
{
  "affected_nodes": ["file::src/week4/cartographer.py"],
  "affected_pipelines": ["week4-lineage-generation"],
  "estimated_records": 251
}
```

### Step 8: Write Violation Record

Append the complete violation record to `violation_log/violations.jsonl` with the blame chain, blast radius, and detection timestamp.

---

## Question 4: Data Contract for LangSmith trace_record

```yaml
kind: DataContract
apiVersion: v3.0.0
id: langsmith-trace-records
info:
  title: LangSmith Trace Records — LLM Run Telemetry
  version: 1.0.0
  owner: platform-team
  description: >
    One record per LLM/chain/tool run captured by LangSmith.
    Used for cost tracking, latency monitoring, and AI contract enforcement.
servers:
  local:
    type: local
    path: outputs/traces/runs.jsonl
    format: jsonl
terms:
  usage: Internal telemetry data. Contains LLM inputs/outputs — treat as sensitive.
  limitations: >
    total_tokens must equal prompt_tokens + completion_tokens.
    total_cost must be non-negative.

# --- Structural Clauses ---
schema:
  id:
    type: string
    format: uuid
    required: true
    unique: true
    description: Primary key. UUIDv4. One per LLM run.
  name:
    type: string
    required: true
    description: Chain or LLM name identifier.
  run_type:
    type: string
    required: true
    enum: [llm, chain, tool, retriever, embedding]
    description: Type of run. Must be one of the five registered types.
  start_time:
    type: string
    format: date-time
    required: true
  end_time:
    type: string
    format: date-time
    required: true
    description: Must be strictly after start_time.
  total_tokens:
    type: integer
    minimum: 0
    required: true
    description: Must equal prompt_tokens + completion_tokens.
  prompt_tokens:
    type: integer
    minimum: 0
    required: true
  completion_tokens:
    type: integer
    minimum: 0
    required: true
  total_cost:
    type: number
    minimum: 0.0
    required: true
    description: Cost in USD. Must be non-negative.
  error:
    type: string
    required: false
    description: Error message if run failed, null otherwise.
  session_id:
    type: string
    format: uuid
    required: true

# --- Statistical Clause ---
quality:
  type: SodaChecks
  specification:
    checks for traces:
      - missing_count(id) = 0
      - duplicate_count(id) = 0
      - min(total_cost) >= 0.0
      - avg(total_tokens) between 500 and 50000
      - max(total_tokens) <= 200000
      - row_count >= 1

# --- AI-Specific Clause ---
ai_contracts:
  token_consistency:
    rule: total_tokens == prompt_tokens + completion_tokens
    severity: CRITICAL
    description: >
      Token counts must be internally consistent. A mismatch indicates
      either a LangSmith export bug or a model API change.
  temporal_ordering:
    rule: end_time > start_time
    severity: CRITICAL
    description: >
      Every run must have a positive duration. Zero or negative duration
      indicates a clock skew or incomplete run record.
  cost_sanity:
    rule: total_cost >= 0 AND total_cost <= 10.0
    severity: HIGH
    description: >
      No single LLM call should cost more than $10. Values above this
      threshold indicate a billing anomaly or runaway token generation.
  error_rate_monitoring:
    baseline_error_rate: 0.05
    warn_threshold: 0.10
    fail_threshold: 0.20
    description: >
      Track the fraction of runs with non-null error field.
      A rising error rate signals model degradation or API instability.

lineage:
  upstream:
    - id: week3-document-refinery
      description: Refinery LLM calls produce trace records
      fields_consumed: [inputs, outputs, total_tokens]
  downstream:
    - id: week7-ai-contract-extensions
      description: AI extensions validate trace schema and monitor drift
      fields_consumed:
        [run_type, total_tokens, prompt_tokens, completion_tokens, total_cost]
      breaking_if_changed: [total_tokens, run_type, total_cost]
```

---

## Question 5: Contract Staleness and Architectural Prevention

### The Most Common Failure Mode

The most common failure mode of contract enforcement in production is **contract staleness** — contracts that were accurate when written but no longer reflect the actual data. This happens because:

1. **Schema changes outpace contract updates.** A developer adds a new field or changes a type in the producer code. The contract YAML is in a separate file (or a separate repo). Nobody remembers to update it. The contract still passes because it only checks fields it knows about — it has no clause for the new field, so it cannot detect that the new field violates an implicit assumption.

2. **Statistical baselines drift naturally.** The baseline mean for `confidence` was 0.768 when the contract was written. Over six months, the extraction model improves and the mean rises to 0.85. The contract's statistical drift check now fires false positives because the baseline is stale. The team disables the check. Six months later, a real drift (the 0–100 scale change) goes undetected because the check was turned off.

3. **Ownership decay.** The person who wrote the contract leaves the team. The contract becomes an artifact that nobody feels responsible for. When it fires a violation, the new team treats it as noise rather than signal.

### How This Architecture Prevents Staleness

**Automated contract generation from live data.** The ContractGenerator runs on the actual JSONL output, not on a hand-written spec. Every time the generator runs, it produces a contract that reflects the current schema. If a field was added, the new contract includes it. If a type changed, the new contract reflects the new type. The SchemaEvolutionAnalyzer then diffs the new contract against the previous snapshot and classifies the change — so the change is detected, classified, and reported rather than silently absorbed.

**Baseline refresh with drift detection.** The ValidationRunner stores baselines in `schema_snapshots/baselines.json` and computes z-scores against them. When a legitimate distribution shift occurs (model improvement), the team can explicitly re-baseline by deleting the baselines file and running the runner again. The deliberate act of re-baselining forces a conscious decision: "yes, the new distribution is correct." This is fundamentally different from silently updating a threshold.

**Lineage-driven blast radius.** Every contract includes a `lineage.downstream[]` section populated from the Week 4 Cartographer's graph. When a contract violation fires, the blast radius report names every affected consumer by ID. This makes staleness visible: if a new downstream consumer is added but the contract's lineage section is not updated, the blast radius report will undercount the impact. The SchemaEvolutionAnalyzer flags this as a lineage coverage gap.

**Temporal snapshots for auditability.** Every ContractGenerator run writes a timestamped snapshot to `schema_snapshots/{contract_id}/{timestamp}.yaml`. This creates an immutable audit trail. You can answer the question "what did the contract look like on January 15th?" by reading the snapshot, not by trusting someone's memory. The SchemaEvolutionAnalyzer diffs consecutive snapshots automatically, so no change goes unrecorded.
