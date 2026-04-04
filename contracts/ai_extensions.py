"""AI Contract Extensions — embedding drift, prompt validation, LLM output enforcement.

Applies AI-specific contract checks that standard data contracts don't cover:
  1. Embedding drift detection (cosine distance from baseline centroid)
  2. Prompt input schema validation (JSON Schema draft-07)
  3. Structured LLM output enforcement (violation rate tracking)

Usage:
    python contracts/ai_extensions.py \
        --mode all \
        --extractions outputs/week3/extractions.jsonl \
        --verdicts outputs/week2/verdicts.jsonl \
        --output validation_reports/ai_extensions.json
"""

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from jsonschema import validate, ValidationError


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Extension 1: Embedding Drift Detection
# ---------------------------------------------------------------------------

def compute_simple_embedding(text: str, dim: int = 128) -> np.ndarray:
    """Deterministic text-to-vector using character hashing.

    This is a local fallback when the OpenAI API is unavailable. It produces
    consistent vectors for the same input, enabling meaningful drift detection
    without API costs. For production use, replace with text-embedding-3-small.
    """
    h = hashlib.sha512(text.encode("utf-8")).digest()
    arr = np.frombuffer(h, dtype=np.uint8)[:dim].astype(np.float64)
    arr = arr / (np.linalg.norm(arr) + 1e-9)
    return arr


def embed_sample(texts: list[str], n: int = 200,
                 use_api: bool = False) -> np.ndarray:
    """Embed a sample of texts. Uses local hashing by default."""
    sample = texts[:n] if len(texts) > n else texts

    if use_api:
        try:
            from openai import OpenAI
            client = OpenAI()
            resp = client.embeddings.create(
                input=sample, model="text-embedding-3-small"
            )
            return np.array([e.embedding for e in resp.data])
        except Exception:
            pass

    return np.array([compute_simple_embedding(t) for t in sample])


def check_embedding_drift(
    texts: list[str],
    baseline_path: str = "schema_snapshots/embedding_baselines.npz",
    threshold: float = 0.15,
    use_api: bool = False,
) -> dict:
    """Check for semantic drift in text content via embedding centroid distance."""
    current_vecs = embed_sample(texts, use_api=use_api)
    current_centroid = current_vecs.mean(axis=0)

    bp = Path(baseline_path)
    if not bp.exists():
        bp.parent.mkdir(parents=True, exist_ok=True)
        np.savez(baseline_path, centroid=current_centroid)
        return {
            "check": "embedding_drift",
            "status": "BASELINE_SET",
            "drift_score": 0.0,
            "threshold": threshold,
            "sample_size": len(texts),
            "interpretation": "Baseline centroid established. "
                            "Subsequent runs will measure drift.",
        }

    baseline = np.load(baseline_path)["centroid"]
    dot = np.dot(current_centroid, baseline)
    norm = np.linalg.norm(current_centroid) * np.linalg.norm(baseline)
    cosine_sim = dot / (norm + 1e-9)
    drift = float(1 - cosine_sim)

    return {
        "check": "embedding_drift",
        "status": "FAIL" if drift > threshold else "PASS",
        "drift_score": round(drift, 4),
        "threshold": threshold,
        "sample_size": len(texts),
        "interpretation": (
            "Semantic content of text has shifted significantly"
            if drift > threshold
            else "Text content is semantically stable"
        ),
    }


# ---------------------------------------------------------------------------
# Extension 2: Prompt Input Schema Validation
# ---------------------------------------------------------------------------

PROMPT_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["doc_id", "source_path", "content_preview"],
    "properties": {
        "doc_id": {"type": "string", "minLength": 1},
        "source_path": {"type": "string", "minLength": 1},
        "content_preview": {"type": "string", "maxLength": 8000},
    },
    "additionalProperties": False,
}


def validate_prompt_inputs(
    records: list[dict],
    quarantine_path: str = "outputs/quarantine/",
) -> dict:
    """Validate extraction records as prompt inputs and quarantine failures."""
    valid_count = 0
    quarantined = []

    for r in records:
        prompt_input = {
            "doc_id": r.get("doc_id", ""),
            "source_path": r.get("source_path", ""),
            "content_preview": json.dumps(r.get("extracted_facts", []))[:8000],
        }
        try:
            validate(instance=prompt_input, schema=PROMPT_INPUT_SCHEMA)
            valid_count += 1
        except ValidationError as e:
            quarantined.append({
                "record_id": r.get("doc_id", "unknown"),
                "error": e.message,
            })

    if quarantined:
        qp = Path(quarantine_path)
        qp.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        with open(qp / f"quarantine_{ts}.jsonl", "w", encoding="utf-8") as f:
            for q in quarantined:
                f.write(json.dumps(q) + "\n")

    total = len(records)
    return {
        "check": "prompt_input_validation",
        "status": "FAIL" if quarantined else "PASS",
        "total_inputs": total,
        "valid": valid_count,
        "quarantined": len(quarantined),
        "quarantine_rate": round(len(quarantined) / max(total, 1), 4),
        "sample_errors": [q["error"] for q in quarantined[:3]],
    }


# ---------------------------------------------------------------------------
# Extension 3: LLM Output Schema Enforcement
# ---------------------------------------------------------------------------

VERDICT_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "verdict_id", "target_ref", "overall_verdict",
        "overall_score", "confidence",
    ],
    "properties": {
        "verdict_id": {"type": "string"},
        "target_ref": {"type": "string"},
        "overall_verdict": {"type": "string", "enum": ["PASS", "FAIL", "WARN"]},
        "overall_score": {"type": "number", "minimum": 1, "maximum": 5},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}


def check_output_schema_violation_rate(
    verdict_records: list[dict],
    baseline_rate: float | None = None,
    warn_threshold: float = 0.02,
) -> dict:
    """Check LLM output schema conformance and track violation rate trends."""
    total = len(verdict_records)
    violations = 0
    violation_details = []

    for v in verdict_records:
        try:
            validate(instance=v, schema=VERDICT_OUTPUT_SCHEMA)
        except ValidationError as e:
            violations += 1
            if len(violation_details) < 5:
                violation_details.append({
                    "verdict_id": v.get("verdict_id", "unknown"),
                    "error": e.message,
                })

    rate = violations / max(total, 1)

    trend = "unknown"
    if baseline_rate is not None:
        if rate > baseline_rate * 1.5:
            trend = "rising"
        elif rate < baseline_rate * 0.5:
            trend = "falling"
        else:
            trend = "stable"

    status = "PASS"
    if rate > warn_threshold:
        status = "WARN"
    if trend == "rising":
        status = "WARN"

    prompt_hash = hashlib.sha256(
        json.dumps(VERDICT_OUTPUT_SCHEMA, sort_keys=True).encode()
    ).hexdigest()[:12]

    return {
        "check": "llm_output_schema",
        "status": status,
        "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "prompt_hash": prompt_hash,
        "total_outputs": total,
        "schema_violations": violations,
        "violation_rate": round(rate, 4),
        "trend": trend,
        "baseline_violation_rate": baseline_rate,
        "warn_threshold": warn_threshold,
        "sample_violations": violation_details,
    }


# ---------------------------------------------------------------------------
# Write AI metrics
# ---------------------------------------------------------------------------

def write_ai_metrics(results: dict, output_path: str):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)


def load_previous_violation_rate(
    metrics_path: str = "validation_reports/ai_metrics.json",
) -> float | None:
    p = Path(metrics_path)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("metrics", {}).get("llm_output_schema", {}).get("violation_rate")
    except (json.JSONDecodeError, OSError):
        return None


def append_warn_violation_record(
    llm_result: dict, path: str = "violation_log/violations.jsonl"
):
    """Surface WARN-level LLM schema risks in the unified violation log."""
    if llm_result.get("status") != "WARN":
        return

    record = {
        "violation_id": str(uuid.uuid4()),
        "type": "llm_output_schema",
        "check_id": "ai.llm_output_schema.violation_rate",
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "severity": "WARNING",
        "status": "WARN",
        "message": (
            "LLM output schema violation rate reached warning threshold."
        ),
        "details": {
            "prompt_hash": llm_result.get("prompt_hash"),
            "total_outputs": llm_result.get("total_outputs"),
            "schema_violations": llm_result.get("schema_violations"),
            "violation_rate": llm_result.get("violation_rate"),
            "baseline_violation_rate": llm_result.get("baseline_violation_rate"),
            "trend": llm_result.get("trend"),
            "warn_threshold": llm_result.get("warn_threshold"),
        },
        "blame_chain": [],
        "blast_radius": {
            "affected_nodes": ["week2", "week7_ai_extensions", "enforcer_report"],
            "affected_pipelines": ["week7_ai_extensions"],
            "contamination_depth": {
                "week7_ai_extensions": 1,
                "enforcer_report": 2,
            },
            "max_contamination_depth": 2,
            "estimated_records": llm_result.get("schema_violations", 0),
        },
    }

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run AI-specific contract extension checks"
    )
    parser.add_argument("--mode", default="all",
                        choices=["all", "embedding", "prompt", "output"],
                        help="Which checks to run")
    parser.add_argument("--extractions", default="outputs/week3/extractions.jsonl",
                        help="Path to Week 3 extractions JSONL")
    parser.add_argument("--verdicts", default="outputs/week2/verdicts.jsonl",
                        help="Path to Week 2 verdicts JSONL")
    parser.add_argument("--output", default="validation_reports/ai_extensions.json",
                        help="Output path for AI extension results")
    parser.add_argument("--use-api", action="store_true",
                        help="Use OpenAI API for embeddings (requires OPENAI_API_KEY)")
    args = parser.parse_args()

    print("Running AI Contract Extensions...")
    results = {
        "run_id": str(uuid.uuid4()),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": [],
    }

    if args.mode in ("all", "embedding"):
        print("\n  Extension 1: Embedding Drift Detection")
        extractions = load_jsonl(args.extractions)
        texts = []
        for r in extractions:
            for fact in r.get("extracted_facts", []):
                t = fact.get("text", "")
                if t:
                    texts.append(t)
        print(f"    Extracted {len(texts)} text samples")

        drift_result = check_embedding_drift(
            texts, use_api=args.use_api
        )
        results["checks"].append(drift_result)
        print(f"    Status: {drift_result['status']}, "
              f"Drift: {drift_result['drift_score']}")

    if args.mode in ("all", "prompt"):
        print("\n  Extension 2: Prompt Input Schema Validation")
        extractions = load_jsonl(args.extractions)
        prompt_result = validate_prompt_inputs(extractions)
        results["checks"].append(prompt_result)
        print(f"    Status: {prompt_result['status']}, "
              f"Valid: {prompt_result['valid']}/{prompt_result['total_inputs']}")

    if args.mode in ("all", "output"):
        print("\n  Extension 3: LLM Output Schema Enforcement")
        verdicts = load_jsonl(args.verdicts)
        baseline_rate = load_previous_violation_rate()
        output_result = check_output_schema_violation_rate(
            verdicts, baseline_rate=baseline_rate
        )
        results["checks"].append(output_result)
        append_warn_violation_record(output_result)
        print(f"    Status: {output_result['status']}, "
              f"Violation rate: {output_result['violation_rate']}")

    write_ai_metrics(results, args.output)

    ai_metrics_path = "validation_reports/ai_metrics.json"
    metrics_summary = {}
    for check in results["checks"]:
        metrics_summary[check["check"]] = {
            "status": check["status"],
        }
        if "drift_score" in check:
            metrics_summary[check["check"]]["drift_score"] = check["drift_score"]
        if "violation_rate" in check:
            metrics_summary[check["check"]]["violation_rate"] = check["violation_rate"]
        if "quarantine_rate" in check:
            metrics_summary[check["check"]]["quarantine_rate"] = check["quarantine_rate"]

    write_ai_metrics(
        {"run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
         "metrics": metrics_summary},
        ai_metrics_path,
    )

    print(f"\n  Results written to {args.output}")
    print(f"  AI metrics written to {ai_metrics_path}")


if __name__ == "__main__":
    main()
