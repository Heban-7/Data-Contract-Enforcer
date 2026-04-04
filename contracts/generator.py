"""ContractGenerator — auto-generates Bitol-compatible data contracts from JSONL data.

Reads a JSONL source file and the Week 4 lineage graph, profiles the data
structurally and statistically, then outputs a Bitol YAML contract, a dbt
schema.yml, and a timestamped schema snapshot.

Usage:
    python contracts/generator.py \
        --source outputs/week3/extractions.jsonl \
        --contract-id week3-document-refinery-extractions \
        --lineage outputs/week4/lineage_snapshots.jsonl \
        --output generated_contracts/
"""

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def flatten_for_profile(records: list[dict]) -> pd.DataFrame:
    """Flatten nested JSONL into a DataFrame suitable for column profiling.

    For records with array fields (extracted_facts, entities, code_refs, etc.)
    we explode the first array field found into one row per item, prefixing
    nested keys with the array field name + underscore.
    """
    array_fields = []
    if records:
        for k, v in records[0].items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                array_fields.append(k)

    rows = []
    for r in records:
        base = {}
        for k, v in r.items():
            if k in array_fields:
                continue
            if isinstance(v, dict):
                for dk, dv in v.items():
                    base[f"{k}_{dk}"] = dv
            else:
                base[k] = v

        if array_fields:
            for af in array_fields:
                for item in r.get(af, [{}]):
                    if isinstance(item, dict):
                        row = {**base}
                        for ik, iv in item.items():
                            if not isinstance(iv, (list, dict)):
                                row[f"{af}_{ik}"] = iv
                        rows.append(row)
        else:
            rows.append(base)

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
# Column profiling
# ---------------------------------------------------------------------------

def profile_column(series: pd.Series, col_name: str) -> dict:
    has_unhashable = False
    try:
        nunique = int(series.nunique())
        sample_vals = [str(v) for v in series.dropna().unique()[:5]]
    except TypeError:
        has_unhashable = True
        str_series = series.apply(lambda x: json.dumps(x, default=str) if isinstance(x, (list, dict)) else x)
        nunique = int(str_series.nunique())
        sample_vals = [str(v) for v in str_series.dropna().unique()[:5]]

    result = {
        "name": col_name,
        "dtype": str(series.dtype),
        "null_fraction": round(float(series.isna().mean()), 4),
        "cardinality_estimate": nunique,
        "sample_values": sample_vals,
    }
    if pd.api.types.is_numeric_dtype(series):
        s = series.dropna()
        if len(s) > 0:
            result["stats"] = {
                "min": round(float(s.min()), 4),
                "max": round(float(s.max()), 4),
                "mean": round(float(s.mean()), 4),
                "p25": round(float(s.quantile(0.25)), 4),
                "p50": round(float(s.quantile(0.50)), 4),
                "p75": round(float(s.quantile(0.75)), 4),
                "p95": round(float(s.quantile(0.95)), 4),
                "p99": round(float(s.quantile(0.99)), 4),
                "stddev": round(float(s.std()), 4),
            }
    return result


# ---------------------------------------------------------------------------
# Contract clause generation
# ---------------------------------------------------------------------------

def infer_type(dtype_str: str) -> str:
    mapping = {
        "float64": "number",
        "float32": "number",
        "int64": "integer",
        "int32": "integer",
        "bool": "boolean",
        "object": "string",
    }
    return mapping.get(dtype_str, "string")


def column_to_clause(profile: dict) -> dict:
    clause = {
        "type": infer_type(profile["dtype"]),
        "required": profile["null_fraction"] == 0.0,
    }

    name = profile["name"]

    if "confidence" in name and clause["type"] == "number":
        clause["minimum"] = 0.0
        clause["maximum"] = 1.0
        clause["description"] = (
            "Confidence score. Must remain 0.0-1.0 float. "
            "BREAKING if changed to 0-100."
        )

    if name.endswith("_id") or name == "id":
        clause["format"] = "uuid"
        clause["pattern"] = "^[0-9a-f-]{36}$"

    if name.endswith("_at") or name.endswith("_time"):
        clause["format"] = "date-time"

    if "hash" in name:
        clause["pattern"] = "^[a-f0-9]{64}$"
        clause["description"] = "SHA-256 hash."

    if profile.get("cardinality_estimate", 999) <= 10 and clause["type"] == "string":
        vals = profile.get("sample_values", [])
        if len(vals) == profile["cardinality_estimate"] and len(vals) > 0:
            clause["enum"] = vals

    if "stats" in profile:
        stats = profile["stats"]
        if "minimum" not in clause:
            clause["minimum"] = stats["min"]
        if "maximum" not in clause:
            clause["maximum"] = stats["max"]

    return clause


def detect_suspicious_distribution(profile: dict) -> list[str]:
    """Flag suspicious numeric distributions that may hide silent corruption."""
    warnings: list[str] = []
    name = profile.get("name", "")
    stats = profile.get("stats", {})
    if not stats:
        return warnings

    col_min = stats.get("min")
    col_max = stats.get("max")
    col_mean = stats.get("mean")
    col_std = stats.get("stddev", 0.0) or 0.0
    p25 = stats.get("p25")
    p75 = stats.get("p75")

    if "confidence" in name and isinstance(col_mean, (int, float)):
        if col_mean > 0.99:
            warnings.append(
                "mean_confidence_gt_0_99: distribution appears clamped near 1.0"
            )
        if col_mean < 0.01:
            warnings.append(
                "mean_confidence_lt_0_01: distribution appears collapsed near 0.0"
            )
        if isinstance(col_max, (int, float)) and col_max > 1.0:
            warnings.append(
                "confidence_max_gt_1_0: possible scale shift from 0.0-1.0 to 0-100"
            )

    if isinstance(col_std, (int, float)) and col_std < 1e-6:
        warnings.append("near_constant_distribution: stddev is near zero")

    if (
        isinstance(p25, (int, float))
        and isinstance(p75, (int, float))
        and p25 == p75
        and isinstance(col_min, (int, float))
        and isinstance(col_max, (int, float))
        and col_min != col_max
    ):
        warnings.append(
            "compressed_iqr: interquartile range collapsed while min/max still vary"
        )

    return warnings


def is_ambiguous_column(profile: dict) -> bool:
    """Heuristic: column meaning isn't obvious from name alone."""
    name = profile.get("name", "")
    dtype = profile.get("dtype", "")
    if dtype not in ("object", "string"):
        return False
    if name.endswith("_id") or name.endswith("_at") or "hash" in name:
        return False
    if name in ("source_path", "codebase_root", "file", "symbol"):
        return False
    if profile.get("cardinality_estimate", 0) <= 1:
        return False
    if profile.get("cardinality_estimate", 0) <= 10 and profile.get("sample_values"):
        return False
    return True


def local_annotation(
    table_name: str,
    column_name: str,
    sample_values: list[str],
    adjacent_columns: list[str],
) -> dict:
    hint = " / ".join(sample_values[:3]) if sample_values else "no samples"
    return {
        "column": column_name,
        "description": (
            f"Likely business descriptor in {table_name}; inferred from sample values: "
            f"{hint}."
        ),
        "business_rule": f"{column_name} should be non-empty and semantically stable.",
        "cross_column_relationship": (
            f"Interpret together with adjacent columns: {adjacent_columns[:5]}"
        ),
        "source": "heuristic_fallback",
    }


def llm_annotation(
    table_name: str,
    column_name: str,
    sample_values: list[str],
    adjacent_columns: list[str],
    model: str = "gpt-4o-mini",
) -> dict:
    """Try LLM annotation; fallback to local heuristic on any failure."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return local_annotation(table_name, column_name, sample_values, adjacent_columns)

    prompt = {
        "table_name": table_name,
        "column_name": column_name,
        "sample_values": sample_values[:5],
        "adjacent_columns": adjacent_columns[:8],
        "task": (
            "Return strict JSON with keys: description, business_rule, "
            "cross_column_relationship."
        ),
    }
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a data contract analyst."},
                {"role": "user", "content": json.dumps(prompt)},
            ],
            temperature=0,
        )
        text = resp.choices[0].message.content if resp.choices else ""
        parsed = json.loads(text) if text else {}
        return {
            "column": column_name,
            "description": parsed.get("description", "").strip() or local_annotation(
                table_name, column_name, sample_values, adjacent_columns
            )["description"],
            "business_rule": parsed.get("business_rule", "").strip()
            or f"{column_name} should follow stable semantics.",
            "cross_column_relationship": parsed.get(
                "cross_column_relationship", ""
            ).strip()
            or f"Correlates with columns: {adjacent_columns[:5]}",
            "source": "llm",
        }
    except Exception:
        return local_annotation(table_name, column_name, sample_values, adjacent_columns)


def annotate_ambiguous_columns(
    table_name: str,
    column_profiles: dict[str, dict],
    enable_llm: bool,
    llm_model: str,
) -> list[dict]:
    annotations: list[dict] = []
    columns = list(column_profiles.keys())
    for idx, col in enumerate(columns):
        profile = column_profiles[col]
        if not is_ambiguous_column(profile):
            continue
        adjacent = []
        if idx > 0:
            adjacent.append(columns[idx - 1])
        if idx + 1 < len(columns):
            adjacent.append(columns[idx + 1])
        annotation = (
            llm_annotation(
                table_name=table_name,
                column_name=col,
                sample_values=profile.get("sample_values", []),
                adjacent_columns=adjacent,
                model=llm_model,
            )
            if enable_llm
            else local_annotation(
                table_name=table_name,
                column_name=col,
                sample_values=profile.get("sample_values", []),
                adjacent_columns=adjacent,
            )
        )
        annotations.append(annotation)
    return annotations


# ---------------------------------------------------------------------------
# Lineage injection
# ---------------------------------------------------------------------------

def load_lineage(lineage_path: str) -> dict | None:
    if not lineage_path or not Path(lineage_path).exists():
        return None
    with open(lineage_path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def inject_lineage(contract: dict, lineage_snapshot: dict | None,
                   contract_id: str) -> dict:
    if lineage_snapshot is None:
        contract["lineage"] = {"upstream": [], "downstream": []}
        return contract

    system_key = contract_id.split("-")[0] if "-" in contract_id else contract_id
    consumers = []
    for edge in lineage_snapshot.get("edges", []):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if system_key in src or "extraction" in src or "event" in src:
            consumers.append({
                "id": tgt,
                "description": f"{tgt} consumes data from {src}",
                "fields_consumed": ["doc_id", "extracted_facts"],
            })

    seen = set()
    unique_consumers = []
    for c in consumers:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique_consumers.append(c)

    contract["lineage"] = {
        "upstream": [],
        "downstream": unique_consumers[:10],
    }
    return contract


# ---------------------------------------------------------------------------
# Bitol contract assembly
# ---------------------------------------------------------------------------

def build_contract(contract_id: str, source_path: str,
                   column_profiles: dict[str, dict],
                   suspicious_warnings: dict[str, list[str]],
                   llm_annotations: list[dict]) -> dict:
    contract = {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": contract_id,
        "info": {
            "title": f"Auto-generated contract for {contract_id}",
            "version": "1.0.0",
            "owner": "data-engineering-team",
            "description": (
                f"Contract generated from {source_path}. "
                f"Covers {len(column_profiles)} fields."
            ),
        },
        "servers": {
            "local": {
                "type": "local",
                "path": source_path,
                "format": "jsonl",
            }
        },
        "terms": {
            "usage": "Internal inter-system data contract. Do not publish.",
            "limitations": "confidence must remain in 0.0-1.0 float range.",
        },
        "schema": {},
        "quality": {
            "type": "SodaChecks",
            "specification": {
                "checks": []
            }
        },
        "profiling_warnings": [],
        "llm_annotations": llm_annotations,
        "profiling": {
            "inferred_from": source_path,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "record_count": 0,
            "column_profiles": column_profiles,
        },
    }

    for col_name, profile in column_profiles.items():
        clause = column_to_clause(profile)
        if suspicious_warnings.get(col_name):
            clause["x_anomaly_hints"] = suspicious_warnings[col_name]
            for warning in suspicious_warnings[col_name]:
                contract["profiling_warnings"].append(
                    {"column": col_name, "warning": warning}
                )
        contract["schema"][col_name] = clause

        if clause.get("required"):
            contract["quality"]["specification"]["checks"].append(
                f"missing_count({col_name}) = 0"
            )
        if clause.get("format") == "uuid" and clause.get("required"):
            contract["quality"]["specification"]["checks"].append(
                f"duplicate_count({col_name}) = 0"
            )
        if "minimum" in clause and "maximum" in clause and clause["type"] == "number":
            contract["quality"]["specification"]["checks"].append(
                f"min({col_name}) >= {clause['minimum']}"
            )
            contract["quality"]["specification"]["checks"].append(
                f"max({col_name}) <= {clause['maximum']}"
            )

    contract["quality"]["specification"]["checks"].append("row_count >= 1")

    return contract


def write_generation_baselines(
    contract_id: str,
    column_profiles: dict[str, dict],
    path: str = "schema_snapshots/baselines.json",
):
    """Persist numeric baselines during generation for downstream drift checks."""
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"written_at": "", "columns": {}}

    data.setdefault("columns", {})
    now = datetime.now(timezone.utc).isoformat()
    for col_name, profile in column_profiles.items():
        stats = profile.get("stats")
        if not stats:
            continue
        data["columns"][col_name] = {
            "mean": float(stats.get("mean", 0.0)),
            "stddev": float(stats.get("stddev", 0.0)),
            "contract_id": contract_id,
            "source": "generator",
            "updated_at": now,
        }
    data["written_at"] = now

    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# dbt schema.yml generation
# ---------------------------------------------------------------------------

def generate_dbt_schema(contract: dict, output_path: Path):
    table_name = contract["id"].replace("-", "_")
    columns = []
    for col_name, clause in contract.get("schema", {}).items():
        col_def = {"name": col_name, "tests": []}
        if clause.get("required"):
            col_def["tests"].append("not_null")
        if clause.get("format") == "uuid":
            col_def["tests"].append("unique")
        if "enum" in clause:
            col_def["tests"].append({
                "accepted_values": {"values": clause["enum"]}
            })
        if col_def["tests"]:
            columns.append(col_def)

    dbt_schema = {
        "version": 2,
        "models": [{
            "name": table_name,
            "description": contract["info"]["description"],
            "columns": columns,
        }]
    }

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(dbt_schema, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Schema snapshot
# ---------------------------------------------------------------------------

def write_snapshot(contract_yaml_path: Path, contract_id: str):
    snapshot_dir = Path("schema_snapshots") / contract_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_path = snapshot_dir / f"{ts}.yaml"
    shutil.copy(contract_yaml_path, snapshot_path)
    print(f"  Snapshot written to {snapshot_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate data contracts from JSONL")
    parser.add_argument("--source", required=True, help="Path to JSONL data file")
    parser.add_argument("--contract-id", required=True, help="Contract identifier")
    parser.add_argument("--lineage", default=None,
                        help="Path to Week 4 lineage_snapshots.jsonl")
    parser.add_argument("--output", required=True, help="Output directory for contracts")
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Enable LLM annotation for ambiguous columns",
    )
    parser.add_argument(
        "--llm-model",
        default="gpt-4o-mini",
        help="LLM model used for annotations when --enable-llm is set",
    )
    parser.add_argument(
        "--include-lineage",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include lineage context in generated contract",
    )
    parser.add_argument(
        "--write-dbt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write dbt schema.yml annotation output",
    )
    parser.add_argument(
        "--write-baselines",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write numeric profiling baselines to schema_snapshots/baselines.json",
    )
    parser.add_argument(
        "--write-snapshot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write timestamped contract snapshot to schema_snapshots/<contract_id>/",
    )
    args = parser.parse_args()

    print(f"Loading data from {args.source}...")
    records = load_jsonl(args.source)
    print(f"  Loaded {len(records)} records")

    print("Flattening and profiling...")
    df = flatten_for_profile(records)
    print(f"  DataFrame shape: {df.shape}")
    print(f"  Columns: {list(df.columns)}")

    column_profiles = {}
    for col in df.columns:
        column_profiles[col] = profile_column(df[col], col)
    suspicious_warnings = {
        col: detect_suspicious_distribution(profile)
        for col, profile in column_profiles.items()
    }
    table_name = Path(args.source).stem
    llm_annotations = annotate_ambiguous_columns(
        table_name=table_name,
        column_profiles=column_profiles,
        enable_llm=args.enable_llm,
        llm_model=args.llm_model,
    )

    print("Building contract...")
    contract = build_contract(
        args.contract_id,
        args.source,
        column_profiles,
        suspicious_warnings,
        llm_annotations,
    )
    contract["profiling"]["record_count"] = len(records)

    if args.include_lineage:
        print("Injecting lineage context...")
        lineage = load_lineage(args.lineage)
        contract = inject_lineage(contract, lineage, args.contract_id)
    else:
        contract["lineage"] = {"upstream": [], "downstream": []}

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = args.contract_id.replace("-", "_").split("_", 1)[-1] if "_" in args.contract_id.replace("-", "_") else args.contract_id.replace("-", "_")
    contract_filename = f"{safe_name}.yaml" if not safe_name.startswith("week") else f"{safe_name}.yaml"

    parts = args.contract_id.split("-")
    for p in parts:
        if p.startswith("week"):
            prefix = p
            rest = args.contract_id.replace(f"{prefix}-", "").replace("-", "_")
            contract_filename = f"{prefix}_{rest}.yaml"
            break
    else:
        contract_filename = f"{args.contract_id.replace('-', '_')}.yaml"

    contract_path = output_dir / contract_filename
    with open(contract_path, "w", encoding="utf-8") as f:
        yaml.dump(contract, f, default_flow_style=False, sort_keys=False,
                  allow_unicode=True)
    print(f"  Contract written to {contract_path}")

    if args.write_dbt:
        dbt_filename = contract_filename.replace(".yaml", "_dbt.yml")
        dbt_path = output_dir / dbt_filename
        generate_dbt_schema(contract, dbt_path)
        print(f"  dbt schema written to {dbt_path}")

    if args.write_baselines:
        write_generation_baselines(args.contract_id, column_profiles)
        print("  Generation baselines written to schema_snapshots/baselines.json")

    if args.write_snapshot:
        write_snapshot(contract_path, args.contract_id)

    total_clauses = len(contract.get("schema", {}))
    print(f"\nContract generation complete: {total_clauses} schema clauses, "
          f"{len(contract['quality']['specification']['checks'])} quality checks")


if __name__ == "__main__":
    main()
