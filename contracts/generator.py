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
import shutil
import uuid
import hashlib
import re
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
                   column_profiles: dict[str, dict]) -> dict:
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
    }

    for col_name, profile in column_profiles.items():
        clause = column_to_clause(profile)
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

    print("Building contract...")
    contract = build_contract(args.contract_id, args.source, column_profiles)

    print("Injecting lineage context...")
    lineage = load_lineage(args.lineage)
    contract = inject_lineage(contract, lineage, args.contract_id)

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

    dbt_filename = contract_filename.replace(".yaml", "_dbt.yml")
    dbt_path = output_dir / dbt_filename
    generate_dbt_schema(contract, dbt_path)
    print(f"  dbt schema written to {dbt_path}")

    write_snapshot(contract_path, args.contract_id)

    total_clauses = len(contract.get("schema", {}))
    print(f"\nContract generation complete: {total_clauses} schema clauses, "
          f"{len(contract['quality']['specification']['checks'])} quality checks")


if __name__ == "__main__":
    main()
