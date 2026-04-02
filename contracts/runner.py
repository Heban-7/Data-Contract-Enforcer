"""ValidationRunner — executes contract checks against a data snapshot.

Runs every clause in a Bitol YAML contract against a JSONL dataset and
produces a structured validation report with PASS/FAIL/WARN/ERROR per check.

Usage:
    python contracts/runner.py \
        --contract generated_contracts/week3_document_refinery_extractions.yaml \
        --data outputs/week3/extractions.jsonl \
        --output validation_reports/week3_run.json
"""

import argparse
import json
import uuid
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml


# ---------------------------------------------------------------------------
# Data loading (shared with generator)
# ---------------------------------------------------------------------------

def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def flatten_for_profile(records: list[dict]) -> pd.DataFrame:
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


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------

def check_required(df: pd.DataFrame, col_name: str, contract_id: str) -> dict:
    """Check that a required field has no nulls."""
    if col_name not in df.columns:
        return {
            "check_id": f"{contract_id}.{col_name}.required",
            "column_name": col_name,
            "check_type": "required",
            "status": "ERROR",
            "actual_value": "column not found",
            "expected": "column exists with no nulls",
            "severity": "CRITICAL",
            "records_failing": 0,
            "sample_failing": [],
            "message": f"Column '{col_name}' not found in dataset.",
        }

    null_count = int(df[col_name].isna().sum())
    total = len(df)
    if null_count > 0:
        sample = df[df[col_name].isna()].index.tolist()[:5]
        return {
            "check_id": f"{contract_id}.{col_name}.required",
            "column_name": col_name,
            "check_type": "required",
            "status": "FAIL",
            "actual_value": f"{null_count}/{total} null",
            "expected": "no nulls",
            "severity": "CRITICAL",
            "records_failing": null_count,
            "sample_failing": [str(s) for s in sample],
            "message": f"{col_name} has {null_count} null values out of {total} records.",
        }

    return {
        "check_id": f"{contract_id}.{col_name}.required",
        "column_name": col_name,
        "check_type": "required",
        "status": "PASS",
        "actual_value": f"0/{total} null",
        "expected": "no nulls",
        "severity": "CRITICAL",
        "records_failing": 0,
        "sample_failing": [],
        "message": f"{col_name} has no null values.",
    }


def check_type(df: pd.DataFrame, col_name: str, expected_type: str,
               contract_id: str) -> dict:
    """Check that a column matches the expected type."""
    if col_name not in df.columns:
        return {
            "check_id": f"{contract_id}.{col_name}.type",
            "column_name": col_name,
            "check_type": "type",
            "status": "ERROR",
            "actual_value": "column not found",
            "expected": expected_type,
            "severity": "CRITICAL",
            "records_failing": 0,
            "sample_failing": [],
            "message": f"Column '{col_name}' not found in dataset.",
        }

    actual_dtype = str(df[col_name].dtype)
    type_ok = False
    if expected_type == "number":
        type_ok = pd.api.types.is_numeric_dtype(df[col_name])
    elif expected_type == "integer":
        type_ok = pd.api.types.is_integer_dtype(df[col_name])
    elif expected_type == "string":
        type_ok = actual_dtype == "object" or pd.api.types.is_string_dtype(df[col_name])
    elif expected_type == "boolean":
        type_ok = pd.api.types.is_bool_dtype(df[col_name])
    else:
        type_ok = True

    status = "PASS" if type_ok else "FAIL"
    return {
        "check_id": f"{contract_id}.{col_name}.type",
        "column_name": col_name,
        "check_type": "type",
        "status": status,
        "actual_value": actual_dtype,
        "expected": expected_type,
        "severity": "CRITICAL",
        "records_failing": 0 if type_ok else len(df),
        "sample_failing": [],
        "message": f"{col_name} dtype is {actual_dtype}, expected {expected_type}.",
    }


def check_enum(df: pd.DataFrame, col_name: str, allowed: list,
               contract_id: str) -> dict:
    """Check that all values are in the allowed enum list."""
    if col_name not in df.columns:
        return {
            "check_id": f"{contract_id}.{col_name}.enum",
            "column_name": col_name,
            "check_type": "enum",
            "status": "ERROR",
            "actual_value": "column not found",
            "expected": f"one of {allowed}",
            "severity": "CRITICAL",
            "records_failing": 0,
            "sample_failing": [],
            "message": f"Column '{col_name}' not found in dataset.",
        }

    non_null = df[col_name].dropna()
    try:
        invalid = non_null[~non_null.isin(allowed)]
    except TypeError:
        str_vals = non_null.apply(lambda x: json.dumps(x, default=str) if isinstance(x, (list, dict)) else str(x))
        invalid = non_null[~str_vals.isin(allowed)]

    count = len(invalid)
    if count > 0:
        sample = [str(v) for v in invalid.unique()[:5]]
        return {
            "check_id": f"{contract_id}.{col_name}.enum",
            "column_name": col_name,
            "check_type": "enum",
            "status": "FAIL",
            "actual_value": f"{count} non-conforming values: {sample}",
            "expected": f"one of {allowed}",
            "severity": "CRITICAL",
            "records_failing": count,
            "sample_failing": sample,
            "message": f"{col_name} has {count} values not in allowed enum.",
        }

    return {
        "check_id": f"{contract_id}.{col_name}.enum",
        "column_name": col_name,
        "check_type": "enum",
        "status": "PASS",
        "actual_value": f"all values in {allowed}",
        "expected": f"one of {allowed}",
        "severity": "CRITICAL",
        "records_failing": 0,
        "sample_failing": [],
        "message": f"{col_name} all values conform to enum.",
    }


def check_pattern(df: pd.DataFrame, col_name: str, pattern: str,
                  contract_id: str) -> dict:
    """Check that string values match a regex pattern."""
    if col_name not in df.columns:
        return {
            "check_id": f"{contract_id}.{col_name}.pattern",
            "column_name": col_name,
            "check_type": "pattern",
            "status": "ERROR",
            "actual_value": "column not found",
            "expected": f"matches {pattern}",
            "severity": "HIGH",
            "records_failing": 0,
            "sample_failing": [],
            "message": f"Column '{col_name}' not found in dataset.",
        }

    non_null = df[col_name].dropna().astype(str)
    compiled = re.compile(pattern)
    invalid = non_null[~non_null.apply(lambda x: bool(compiled.match(x)))]
    count = len(invalid)

    if count > 0:
        sample = invalid.head(5).tolist()
        return {
            "check_id": f"{contract_id}.{col_name}.pattern",
            "column_name": col_name,
            "check_type": "pattern",
            "status": "FAIL",
            "actual_value": f"{count} values don't match pattern",
            "expected": f"matches {pattern}",
            "severity": "HIGH",
            "records_failing": count,
            "sample_failing": sample,
            "message": f"{col_name} has {count} values not matching {pattern}.",
        }

    return {
        "check_id": f"{contract_id}.{col_name}.pattern",
        "column_name": col_name,
        "check_type": "pattern",
        "status": "PASS",
        "actual_value": f"all {len(non_null)} values match",
        "expected": f"matches {pattern}",
        "severity": "HIGH",
        "records_failing": 0,
        "sample_failing": [],
        "message": f"{col_name} all values match pattern.",
    }


def check_datetime(df: pd.DataFrame, col_name: str, contract_id: str) -> dict:
    """Check that values parse as ISO 8601 datetime."""
    if col_name not in df.columns:
        return {
            "check_id": f"{contract_id}.{col_name}.datetime",
            "column_name": col_name,
            "check_type": "datetime",
            "status": "ERROR",
            "actual_value": "column not found",
            "expected": "ISO 8601 datetime",
            "severity": "HIGH",
            "records_failing": 0,
            "sample_failing": [],
            "message": f"Column '{col_name}' not found in dataset.",
        }

    non_null = df[col_name].dropna().astype(str)
    bad = []
    for val in non_null:
        try:
            datetime.fromisoformat(val.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            bad.append(val)

    if bad:
        return {
            "check_id": f"{contract_id}.{col_name}.datetime",
            "column_name": col_name,
            "check_type": "datetime",
            "status": "FAIL",
            "actual_value": f"{len(bad)} unparseable values",
            "expected": "ISO 8601 datetime",
            "severity": "HIGH",
            "records_failing": len(bad),
            "sample_failing": bad[:5],
            "message": f"{col_name} has {len(bad)} values that don't parse as ISO 8601.",
        }

    return {
        "check_id": f"{contract_id}.{col_name}.datetime",
        "column_name": col_name,
        "check_type": "datetime",
        "status": "PASS",
        "actual_value": f"all {len(non_null)} values parse",
        "expected": "ISO 8601 datetime",
        "severity": "HIGH",
        "records_failing": 0,
        "sample_failing": [],
        "message": f"{col_name} all values are valid ISO 8601.",
    }


def check_range(df: pd.DataFrame, col_name: str, minimum: float,
                maximum: float, contract_id: str) -> dict:
    """Check that numeric values fall within [minimum, maximum]."""
    if col_name not in df.columns:
        return {
            "check_id": f"{contract_id}.{col_name}.range",
            "column_name": col_name,
            "check_type": "range",
            "status": "ERROR",
            "actual_value": "column not found",
            "expected": f"min>={minimum}, max<={maximum}",
            "severity": "CRITICAL",
            "records_failing": 0,
            "sample_failing": [],
            "message": f"Column '{col_name}' not found in dataset.",
        }

    if not pd.api.types.is_numeric_dtype(df[col_name]):
        return {
            "check_id": f"{contract_id}.{col_name}.range",
            "column_name": col_name,
            "check_type": "range",
            "status": "ERROR",
            "actual_value": f"dtype={df[col_name].dtype}",
            "expected": f"numeric, min>={minimum}, max<={maximum}",
            "severity": "CRITICAL",
            "records_failing": 0,
            "sample_failing": [],
            "message": f"{col_name} is not numeric, cannot check range.",
        }

    s = df[col_name].dropna()
    data_min = float(s.min())
    data_max = float(s.max())
    data_mean = float(s.mean())

    violations = s[(s < minimum) | (s > maximum)]
    count = len(violations)

    if count > 0:
        sample_ids = []
        for idx in violations.index[:5]:
            for id_col in ["doc_id", "event_id", "intent_id", "id", "verdict_id"]:
                if id_col in df.columns:
                    sample_ids.append(str(df.loc[idx, id_col]))
                    break
            else:
                sample_ids.append(str(idx))

        return {
            "check_id": f"{contract_id}.{col_name}.range",
            "column_name": col_name,
            "check_type": "range",
            "status": "FAIL",
            "actual_value": f"max={data_max}, mean={round(data_mean, 1)}",
            "expected": f"max<={maximum}, min>={minimum}",
            "severity": "CRITICAL",
            "records_failing": count,
            "sample_failing": sample_ids,
            "message": (
                f"{col_name} is in {data_min:.1f}-{data_max:.1f} range, "
                f"not {minimum}-{maximum}. Breaking change detected."
            ),
        }

    return {
        "check_id": f"{contract_id}.{col_name}.range",
        "column_name": col_name,
        "check_type": "range",
        "status": "PASS",
        "actual_value": f"min={data_min}, max={data_max}",
        "expected": f"min>={minimum}, max<={maximum}",
        "severity": "CRITICAL",
        "records_failing": 0,
        "sample_failing": [],
        "message": f"{col_name} all values within [{minimum}, {maximum}].",
    }


def check_statistical_drift(col_name: str, current_mean: float,
                            current_std: float, baselines: dict,
                            contract_id: str) -> dict | None:
    """Check for statistical drift against stored baselines."""
    cols = baselines.get("columns", {})
    if col_name not in cols:
        return None

    b = cols[col_name]
    baseline_std = max(b.get("stddev", 1e-9), 1e-9)
    z_score = abs(current_mean - b["mean"]) / baseline_std

    if z_score > 3:
        status = "FAIL"
        severity = "HIGH"
    elif z_score > 2:
        status = "WARN"
        severity = "MEDIUM"
    else:
        status = "PASS"
        severity = "LOW"

    return {
        "check_id": f"{contract_id}.{col_name}.statistical_drift",
        "column_name": col_name,
        "check_type": "statistical_drift",
        "status": status,
        "actual_value": f"mean={round(current_mean, 4)}, z={round(z_score, 2)}",
        "expected": f"mean={round(b['mean'], 4)} +/- 3*{round(baseline_std, 4)}",
        "severity": severity,
        "records_failing": 0,
        "sample_failing": [],
        "message": f"{col_name} mean drifted {z_score:.1f} stddev from baseline.",
    }


# ---------------------------------------------------------------------------
# Baseline management
# ---------------------------------------------------------------------------

def load_baselines(path: str = "schema_snapshots/baselines.json") -> dict:
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def write_baselines(df: pd.DataFrame,
                    path: str = "schema_snapshots/baselines.json"):
    baselines = {"written_at": datetime.now(timezone.utc).isoformat(), "columns": {}}
    for col in df.select_dtypes(include="number").columns:
        s = df[col].dropna()
        if len(s) > 0:
            baselines["columns"][col] = {
                "mean": round(float(s.mean()), 6),
                "stddev": round(float(s.std()), 6),
            }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baselines, f, indent=2)
    print(f"  Baselines written to {path}")


# ---------------------------------------------------------------------------
# Main validation pipeline
# ---------------------------------------------------------------------------

def run_validation(contract_path: str, data_path: str) -> dict:
    with open(contract_path, encoding="utf-8") as f:
        contract = yaml.safe_load(f)

    records = load_jsonl(data_path)
    df = flatten_for_profile(records)

    contract_id = contract.get("id", "unknown")
    results = []

    schema = contract.get("schema", {})
    for col_name, clause in schema.items():
        try:
            if clause.get("required"):
                results.append(check_required(df, col_name, contract_id))

            if "type" in clause:
                results.append(check_type(df, col_name, clause["type"], contract_id))

            if "enum" in clause:
                results.append(check_enum(df, col_name, clause["enum"], contract_id))

            if "pattern" in clause:
                results.append(check_pattern(df, col_name, clause["pattern"], contract_id))

            if clause.get("format") == "date-time":
                results.append(check_datetime(df, col_name, contract_id))

            if "minimum" in clause and "maximum" in clause and clause.get("type") in ("number", "integer"):
                results.append(check_range(
                    df, col_name, clause["minimum"], clause["maximum"], contract_id
                ))
        except Exception as e:
            results.append({
                "check_id": f"{contract_id}.{col_name}.error",
                "column_name": col_name,
                "check_type": "error",
                "status": "ERROR",
                "actual_value": str(e),
                "expected": "check should execute",
                "severity": "HIGH",
                "records_failing": 0,
                "sample_failing": [],
                "message": f"Check failed with error: {e}",
            })

    baselines = load_baselines()
    for col in df.select_dtypes(include="number").columns:
        s = df[col].dropna()
        if len(s) > 0:
            drift_result = check_statistical_drift(
                col, float(s.mean()), float(s.std()), baselines, contract_id
            )
            if drift_result:
                results.append(drift_result)

    if not baselines.get("columns"):
        write_baselines(df)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    warned = sum(1 for r in results if r["status"] == "WARN")
    errored = sum(1 for r in results if r["status"] == "ERROR")

    report = {
        "report_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "snapshot_id": file_sha256(data_path),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_checks": len(results),
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "errored": errored,
        "results": results,
    }

    return report


def main():
    parser = argparse.ArgumentParser(description="Run contract validation checks")
    parser.add_argument("--contract", required=True, help="Path to contract YAML")
    parser.add_argument("--data", required=True, help="Path to JSONL data file")
    parser.add_argument("--output", required=True, help="Output path for report JSON")
    args = parser.parse_args()

    print(f"Running validation...")
    print(f"  Contract: {args.contract}")
    print(f"  Data: {args.data}")

    report = run_validation(args.contract, args.data)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nValidation complete:")
    print(f"  Total checks: {report['total_checks']}")
    print(f"  Passed: {report['passed']}")
    print(f"  Failed: {report['failed']}")
    print(f"  Warned: {report['warned']}")
    print(f"  Errored: {report['errored']}")
    print(f"  Report written to {args.output}")


if __name__ == "__main__":
    main()
