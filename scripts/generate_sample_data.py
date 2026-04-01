"""Generate realistic sample JSONL data for all 5 weeks + LangSmith traces.

Produces files conforming to the canonical schemas defined in the project spec.
Each file contains 50+ records with realistic distributions.
"""

import json
import uuid
import random
import hashlib
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(42)

BASE = Path(__file__).resolve().parent.parent / "outputs"

PYTHON_FILES = [
    "src/main.py", "src/utils.py", "src/extractor.py", "src/parser.py",
    "src/models/document.py", "src/models/entity.py", "src/pipeline.py",
    "src/config.py", "src/validators.py", "src/api/routes.py",
    "src/api/handlers.py", "src/services/llm.py", "src/services/storage.py",
    "tests/test_extractor.py", "tests/test_parser.py",
]

GOVERNANCE_TAGS = ["auth", "pii", "billing", "compliance", "security", "logging"]
ENTITY_TYPES = ["PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"]
NODE_TYPES = ["FILE", "TABLE", "SERVICE", "MODEL", "PIPELINE", "EXTERNAL"]
EDGE_RELATIONSHIPS = ["IMPORTS", "CALLS", "READS", "WRITES", "PRODUCES", "CONSUMES"]
VERDICT_VALUES = ["PASS", "FAIL", "WARN"]
RUN_TYPES = ["llm", "chain", "tool", "retriever", "embedding"]

SAMPLE_FACTS = [
    "The company reported revenue of $4.2 billion in Q3 2024.",
    "Employee count grew by 15% year-over-year.",
    "The merger is expected to close by end of fiscal year 2025.",
    "Operating margins improved to 23.4% from 19.8%.",
    "Customer retention rate stands at 94.2%.",
    "The new product launch is scheduled for March 2025.",
    "R&D spending increased to $890 million.",
    "Market share in the enterprise segment reached 31%.",
    "The board approved a $2 billion share buyback program.",
    "Cloud revenue grew 42% to reach $1.8 billion.",
    "Total debt was reduced by $500 million during the quarter.",
    "The patent portfolio expanded to 3,400 active patents.",
    "International revenue accounted for 38% of total sales.",
    "The CEO announced plans to expand into three new markets.",
    "Gross profit margin was 67.3% for the reporting period.",
]

ENTITY_NAMES = [
    ("Acme Corp", "ORG"), ("Jane Smith", "PERSON"), ("New York", "LOCATION"),
    ("Q3 2024", "DATE"), ("$4.2 billion", "AMOUNT"), ("John Doe", "PERSON"),
    ("London", "LOCATION"), ("TechCo Inc", "ORG"), ("2025-03-15", "DATE"),
    ("$890 million", "AMOUNT"), ("Sarah Johnson", "PERSON"),
    ("San Francisco", "LOCATION"), ("Global Industries", "ORG"),
    ("FY2025", "DATE"), ("15%", "AMOUNT"),
]

RUBRIC_CRITERIA = ["clarity", "completeness", "accuracy", "relevance", "coherence"]


def uid():
    return str(uuid.uuid4())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def iso_ts(base: datetime, offset_minutes: int = 0) -> str:
    return (base + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def random_ts(start: datetime, span_days: int = 14) -> datetime:
    return start + timedelta(seconds=random.randint(0, span_days * 86400))


# ---------------------------------------------------------------------------
# Week 1 -- Intent-Code Correlator
# ---------------------------------------------------------------------------
def generate_week1(n: int = 55):
    base_time = datetime(2025, 1, 10, tzinfo=timezone.utc)
    records = []
    for _ in range(n):
        ts = random_ts(base_time)
        num_refs = random.randint(1, 4)
        code_refs = []
        for _ in range(num_refs):
            f = random.choice(PYTHON_FILES)
            ls = random.randint(1, 200)
            code_refs.append({
                "file": f,
                "line_start": ls,
                "line_end": ls + random.randint(5, 40),
                "symbol": random.choice(["process_document", "extract_entities",
                                         "validate_input", "run_pipeline",
                                         "parse_config", "handle_request"]),
                "confidence": round(random.uniform(0.55, 0.99), 2),
            })
        records.append({
            "intent_id": uid(),
            "description": random.choice([
                "Extract named entities from uploaded PDF documents",
                "Validate user authentication tokens before processing",
                "Transform raw CSV data into normalized schema",
                "Generate summary report from extracted facts",
                "Route incoming events to appropriate handlers",
            ]),
            "code_refs": code_refs,
            "governance_tags": random.sample(GOVERNANCE_TAGS, random.randint(1, 3)),
            "created_at": iso_ts(ts),
        })
    write_jsonl(BASE / "week1" / "intent_records.jsonl", records)
    return records


# ---------------------------------------------------------------------------
# Week 2 -- Digital Courtroom
# ---------------------------------------------------------------------------
def generate_week2(n: int = 55):
    base_time = datetime(2025, 1, 11, tzinfo=timezone.utc)
    rubric_yaml = "rubric:\n  criteria:\n    - clarity\n    - completeness\n"
    rubric_id = sha256(rubric_yaml)
    records = []
    for _ in range(n):
        ts = random_ts(base_time)
        scores = {}
        for criterion in RUBRIC_CRITERIA:
            scores[criterion] = {
                "score": random.randint(1, 5),
                "evidence": [random.choice(SAMPLE_FACTS)],
                "notes": f"Evaluation of {criterion} dimension.",
            }
        raw_scores = [s["score"] for s in scores.values()]
        records.append({
            "verdict_id": uid(),
            "target_ref": random.choice(PYTHON_FILES),
            "rubric_id": rubric_id,
            "rubric_version": "1.2.0",
            "scores": scores,
            "overall_verdict": random.choice(VERDICT_VALUES),
            "overall_score": round(sum(raw_scores) / len(raw_scores), 1),
            "confidence": round(random.uniform(0.60, 0.99), 2),
            "evaluated_at": iso_ts(ts),
        })
    write_jsonl(BASE / "week2" / "verdicts.jsonl", records)
    return records


# ---------------------------------------------------------------------------
# Week 3 -- Document Refinery
# ---------------------------------------------------------------------------
def generate_week3(n: int = 60):
    base_time = datetime(2025, 1, 12, tzinfo=timezone.utc)
    records = []
    for i in range(n):
        ts = random_ts(base_time)
        doc_id = uid()
        source_text = f"Sample document content #{i} for extraction pipeline."
        entities = []
        num_entities = random.randint(2, 5)
        for _ in range(num_entities):
            name, etype = random.choice(ENTITY_NAMES)
            entities.append({
                "entity_id": uid(),
                "name": name,
                "type": etype,
                "canonical_value": name,
            })
        entity_ids = [e["entity_id"] for e in entities]
        facts = []
        num_facts = random.randint(2, 6)
        for _ in range(num_facts):
            facts.append({
                "fact_id": uid(),
                "text": random.choice(SAMPLE_FACTS),
                "entity_refs": random.sample(entity_ids, min(2, len(entity_ids))),
                "confidence": round(random.uniform(0.55, 0.98), 2),
                "page_ref": random.choice([None, random.randint(1, 20)]),
                "source_excerpt": random.choice(SAMPLE_FACTS),
            })
        records.append({
            "doc_id": doc_id,
            "source_path": f"https://docs.example.com/reports/doc_{i:04d}.pdf",
            "source_hash": sha256(source_text),
            "extracted_facts": facts,
            "entities": entities,
            "extraction_model": random.choice([
                "claude-3-5-sonnet-20241022", "claude-3-haiku-20240307",
                "gpt-4-turbo-2024-04-09",
            ]),
            "processing_time_ms": random.randint(400, 3500),
            "token_count": {
                "input": random.randint(2000, 8000),
                "output": random.randint(300, 2000),
            },
            "extracted_at": iso_ts(ts),
        })
    write_jsonl(BASE / "week3" / "extractions.jsonl", records)
    return records


# ---------------------------------------------------------------------------
# Week 4 -- Brownfield Cartographer
# ---------------------------------------------------------------------------
def generate_week4(n_snapshots: int = 2):
    base_time = datetime(2025, 1, 13, tzinfo=timezone.utc)
    records = []
    for s in range(n_snapshots):
        ts = base_time + timedelta(days=s * 3)
        nodes = []
        for f in PYTHON_FILES:
            nodes.append({
                "node_id": f"file::{f}",
                "type": "FILE",
                "label": f.split("/")[-1],
                "metadata": {
                    "path": f,
                    "language": "python",
                    "purpose": f"Handles {f.split('/')[-1].replace('.py', '')} logic",
                    "last_modified": iso_ts(ts, -random.randint(0, 10000)),
                },
            })
        for extra in ["table::extractions", "table::events", "service::week3-refinery",
                       "service::week5-event-platform", "pipeline::extraction-pipeline",
                       "pipeline::event-pipeline", "external::langsmith"]:
            ntype = extra.split("::")[0].upper()
            if ntype == "TABLE":
                ntype = "TABLE"
            elif ntype == "SERVICE":
                ntype = "SERVICE"
            elif ntype == "PIPELINE":
                ntype = "PIPELINE"
            elif ntype == "EXTERNAL":
                ntype = "EXTERNAL"
            nodes.append({
                "node_id": extra,
                "type": ntype,
                "label": extra.split("::")[-1],
                "metadata": {
                    "path": extra,
                    "language": "n/a",
                    "purpose": f"{extra.split('::')[-1]} component",
                    "last_modified": iso_ts(ts),
                },
            })
        edges = []
        for i in range(len(PYTHON_FILES) - 1):
            edges.append({
                "source": f"file::{PYTHON_FILES[i]}",
                "target": f"file::{PYTHON_FILES[i + 1]}",
                "relationship": random.choice(EDGE_RELATIONSHIPS),
                "confidence": round(random.uniform(0.70, 0.99), 2),
            })
        edges.extend([
            {"source": "file::src/extractor.py", "target": "table::extractions",
             "relationship": "WRITES", "confidence": 0.95},
            {"source": "table::extractions", "target": "service::week3-refinery",
             "relationship": "CONSUMES", "confidence": 0.92},
            {"source": "service::week3-refinery", "target": "pipeline::extraction-pipeline",
             "relationship": "PRODUCES", "confidence": 0.90},
            {"source": "pipeline::extraction-pipeline", "target": "external::langsmith",
             "relationship": "WRITES", "confidence": 0.88},
            {"source": "file::src/pipeline.py", "target": "table::events",
             "relationship": "WRITES", "confidence": 0.93},
            {"source": "table::events", "target": "service::week5-event-platform",
             "relationship": "CONSUMES", "confidence": 0.91},
            {"source": "service::week5-event-platform", "target": "pipeline::event-pipeline",
             "relationship": "PRODUCES", "confidence": 0.89},
        ])
        git_commit = hashlib.sha1(f"commit-{s}".encode()).hexdigest()
        records.append({
            "snapshot_id": uid(),
            "codebase_root": "/repo/week3-document-refinery",
            "git_commit": git_commit,
            "nodes": nodes,
            "edges": edges,
            "captured_at": iso_ts(ts),
        })
    write_jsonl(BASE / "week4" / "lineage_snapshots.jsonl", records)
    return records


# ---------------------------------------------------------------------------
# Week 5 -- Event Sourcing Platform
# ---------------------------------------------------------------------------
def generate_week5(n: int = 60):
    base_time = datetime(2025, 1, 14, tzinfo=timezone.utc)
    event_types = [
        "DocumentProcessed", "EntityExtracted", "FactValidated",
        "PipelineStarted", "PipelineCompleted", "SchemaUpdated",
        "UserAuthenticated", "ReportGenerated",
    ]
    aggregate_types = ["Document", "Entity", "Pipeline", "User", "Report"]
    aggregates = {uid(): random.choice(aggregate_types) for _ in range(10)}
    records = []
    seq_counters = {aid: 0 for aid in aggregates}
    for _ in range(n):
        ts = random_ts(base_time)
        agg_id = random.choice(list(aggregates.keys()))
        seq_counters[agg_id] += 1
        occurred = ts
        recorded = occurred + timedelta(seconds=random.randint(0, 5))
        records.append({
            "event_id": uid(),
            "event_type": random.choice(event_types),
            "aggregate_id": agg_id,
            "aggregate_type": aggregates[agg_id],
            "sequence_number": seq_counters[agg_id],
            "payload": {
                "detail": f"Event payload for sequence {seq_counters[agg_id]}",
                "processed_items": random.randint(1, 100),
            },
            "metadata": {
                "causation_id": random.choice([uid(), None]),
                "correlation_id": uid(),
                "user_id": f"user_{random.randint(1, 20):03d}",
                "source_service": random.choice([
                    "week3-document-refinery", "week5-event-platform",
                    "week1-intent-correlator",
                ]),
            },
            "schema_version": "1.0",
            "occurred_at": iso_ts(occurred),
            "recorded_at": iso_ts(recorded),
        })
    write_jsonl(BASE / "week5" / "events.jsonl", records)
    return records


# ---------------------------------------------------------------------------
# LangSmith Traces
# ---------------------------------------------------------------------------
def generate_traces(n: int = 55):
    base_time = datetime(2025, 1, 15, tzinfo=timezone.utc)
    chain_names = [
        "extraction_chain", "entity_resolver", "fact_validator",
        "summary_generator", "document_classifier", "embedding_pipeline",
    ]
    records = []
    session_id = uid()
    for _ in range(n):
        ts = random_ts(base_time)
        start = ts
        duration_ms = random.randint(200, 8000)
        end = start + timedelta(milliseconds=duration_ms)
        prompt_tokens = random.randint(1000, 8000)
        completion_tokens = random.randint(200, 2000)
        total_tokens = prompt_tokens + completion_tokens
        cost_per_token = 0.000003
        records.append({
            "id": uid(),
            "name": random.choice(chain_names),
            "run_type": random.choice(RUN_TYPES),
            "inputs": {"query": random.choice(SAMPLE_FACTS)},
            "outputs": {"result": "Processed successfully"},
            "error": random.choice([None, None, None, None, "TimeoutError: LLM call exceeded 30s"]),
            "start_time": iso_ts(start),
            "end_time": iso_ts(end),
            "total_tokens": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_cost": round(total_tokens * cost_per_token, 4),
            "tags": random.sample(["week3", "extraction", "week5", "events",
                                    "production", "staging"], 2),
            "parent_run_id": random.choice([uid(), None]),
            "session_id": session_id,
        })
    write_jsonl(BASE / "traces" / "runs.jsonl", records)
    return records


def write_jsonl(path: Path, records: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"  Wrote {len(records)} records to {path}")


if __name__ == "__main__":
    print("Generating sample data...")
    generate_week1()
    generate_week2()
    generate_week3()
    generate_week4()
    generate_week5()
    generate_traces()
    print("Done.")
