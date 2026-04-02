"""SchemaEvolutionAnalyzer — diffs schema snapshots and classifies changes.

Compares consecutive timestamped schema snapshots for a given contract,
classifies each change using the backward/forward compatibility taxonomy,
and generates a migration impact report.

Usage:
    python contracts/schema_analyzer.py \
        --contract-id week3-document-refinery-extractions \
        --since "7 days ago" \
        --output validation_reports/schema_evolution_week3.json
"""

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------

def load_snapshots(contract_id: str) -> list[tuple[str, dict]]:
    """Load all timestamped snapshots for a contract, sorted by time."""
    snapshot_dir = Path("schema_snapshots") / contract_id
    if not snapshot_dir.exists():
        return []

    snapshots = []
    for p in sorted(snapshot_dir.glob("*.yaml")):
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        snapshots.append((p.name, data))

    return snapshots


# ---------------------------------------------------------------------------
# Change classification
# ---------------------------------------------------------------------------

def classify_change(field_name: str, old_clause: dict | None,
                    new_clause: dict | None) -> tuple[str, str, str]:
    """Classify a schema change. Returns (verdict, change_type, description)."""

    if old_clause is None and new_clause is not None:
        if new_clause.get("required", False):
            return (
                "BREAKING",
                "add_non_nullable_column",
                f"New required field '{field_name}' added. "
                "All producers must supply this field."
            )
        return (
            "COMPATIBLE",
            "add_nullable_column",
            f"New optional field '{field_name}' added. "
            "Consumers can safely ignore it."
        )

    if old_clause is not None and new_clause is None:
        return (
            "BREAKING",
            "remove_column",
            f"Field '{field_name}' removed. "
            "Deprecation period mandatory (minimum 2 sprints)."
        )

    if old_clause is None or new_clause is None:
        return ("COMPATIBLE", "no_change", "No material change.")

    changes = []

    if old_clause.get("type") != new_clause.get("type"):
        old_t = old_clause.get("type", "unknown")
        new_t = new_clause.get("type", "unknown")
        widening = {
            ("integer", "number"),
            ("float32", "float64"),
            ("int32", "int64"),
        }
        if (old_t, new_t) in widening:
            changes.append((
                "COMPATIBLE",
                "type_widening",
                f"Type widened from {old_t} to {new_t}. "
                "Validate no precision loss."
            ))
        else:
            changes.append((
                "BREAKING",
                "type_narrowing",
                f"Type changed from {old_t} to {new_t}. "
                "CRITICAL — requires migration plan with rollback."
            ))

    old_max = old_clause.get("maximum")
    new_max = new_clause.get("maximum")
    if old_max is not None and new_max is not None and old_max != new_max:
        changes.append((
            "BREAKING",
            "range_change",
            f"Maximum changed from {old_max} to {new_max}. "
            "Statistical baseline must be re-established."
        ))

    old_min = old_clause.get("minimum")
    new_min = new_clause.get("minimum")
    if old_min is not None and new_min is not None and old_min != new_min:
        changes.append((
            "BREAKING",
            "range_change",
            f"Minimum changed from {old_min} to {new_min}."
        ))

    old_enum = set(old_clause.get("enum", []))
    new_enum = set(new_clause.get("enum", []))
    if old_enum != new_enum:
        added = new_enum - old_enum
        removed = old_enum - new_enum
        if removed:
            changes.append((
                "BREAKING",
                "enum_removal",
                f"Enum values removed: {removed}. "
                "Treat as breaking change."
            ))
        if added and not removed:
            changes.append((
                "COMPATIBLE",
                "enum_addition",
                f"Enum values added: {added}. Notify consumers."
            ))

    if old_clause.get("required") != new_clause.get("required"):
        if new_clause.get("required") and not old_clause.get("required"):
            changes.append((
                "BREAKING",
                "required_change",
                f"Field '{field_name}' changed from optional to required."
            ))
        else:
            changes.append((
                "COMPATIBLE",
                "required_change",
                f"Field '{field_name}' changed from required to optional."
            ))

    if old_clause.get("pattern") != new_clause.get("pattern"):
        changes.append((
            "BREAKING",
            "pattern_change",
            f"Pattern changed from '{old_clause.get('pattern')}' "
            f"to '{new_clause.get('pattern')}'."
        ))

    if not changes:
        return ("COMPATIBLE", "no_change", "No material change detected.")

    worst = "COMPATIBLE"
    for verdict, _, _ in changes:
        if verdict == "BREAKING":
            worst = "BREAKING"
            break

    all_descriptions = "; ".join(d for _, _, d in changes)
    all_types = ", ".join(t for _, t, _ in changes)
    return (worst, all_types, all_descriptions)


# ---------------------------------------------------------------------------
# Schema diffing
# ---------------------------------------------------------------------------

def diff_schemas(old_schema: dict, new_schema: dict) -> list[dict]:
    """Diff two schema dictionaries field by field."""
    all_fields = set(list(old_schema.keys()) + list(new_schema.keys()))
    changes = []

    for field in sorted(all_fields):
        old_clause = old_schema.get(field)
        new_clause = new_schema.get(field)

        if old_clause == new_clause:
            continue

        verdict, change_type, description = classify_change(
            field, old_clause, new_clause
        )
        changes.append({
            "field": field,
            "verdict": verdict,
            "change_type": change_type,
            "description": description,
            "old_value": old_clause,
            "new_value": new_clause,
        })

    return changes


# ---------------------------------------------------------------------------
# Migration impact report
# ---------------------------------------------------------------------------

def generate_migration_report(contract_id: str, changes: list[dict],
                              old_name: str, new_name: str) -> dict:
    """Generate a migration impact report for detected changes."""
    breaking = [c for c in changes if c["verdict"] == "BREAKING"]
    compatible = [c for c in changes if c["verdict"] == "COMPATIBLE"]

    overall = "BREAKING" if breaking else "COMPATIBLE"

    migration_steps = []
    rollback_steps = []
    for i, change in enumerate(breaking, 1):
        migration_steps.append(
            f"{i}. Update all producers to handle '{change['field']}': "
            f"{change['description']}"
        )
        rollback_steps.append(
            f"{i}. Revert '{change['field']}' to previous schema: "
            f"{json.dumps(change['old_value'], default=str)}"
        )

    affected_consumers = []
    for change in breaking:
        affected_consumers.append({
            "field": change["field"],
            "failure_mode": f"Consumer reads '{change['field']}' with old schema. "
                          f"After change: {change['description']}",
        })

    return {
        "report_id": str(uuid.uuid4()),
        "contract_id": contract_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_old": old_name,
        "snapshot_new": new_name,
        "overall_verdict": overall,
        "total_changes": len(changes),
        "breaking_changes": len(breaking),
        "compatible_changes": len(compatible),
        "changes": changes,
        "migration_checklist": migration_steps if migration_steps else [
            "No migration required — all changes are backward compatible."
        ],
        "rollback_plan": rollback_steps if rollback_steps else [
            "No rollback needed."
        ],
        "per_consumer_failure_analysis": affected_consumers,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze schema evolution between snapshots"
    )
    parser.add_argument("--contract-id", required=True,
                        help="Contract identifier")
    parser.add_argument("--since", default="7 days ago",
                        help="Time window for analysis")
    parser.add_argument("--output", required=True,
                        help="Output path for evolution report JSON")
    args = parser.parse_args()

    print(f"Analyzing schema evolution for {args.contract_id}...")

    snapshots = load_snapshots(args.contract_id)
    if len(snapshots) < 2:
        print(f"  Only {len(snapshots)} snapshot(s) found. "
              "Need at least 2 to diff.")
        report = {
            "report_id": str(uuid.uuid4()),
            "contract_id": args.contract_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall_verdict": "NO_DIFF",
            "message": f"Only {len(snapshots)} snapshot(s) available. "
                       "Run the generator multiple times to produce diffs.",
            "total_changes": 0,
            "changes": [],
        }
    else:
        old_name, old_data = snapshots[-2]
        new_name, new_data = snapshots[-1]
        print(f"  Comparing {old_name} -> {new_name}")

        old_schema = old_data.get("schema", {})
        new_schema = new_data.get("schema", {})

        changes = diff_schemas(old_schema, new_schema)
        report = generate_migration_report(
            args.contract_id, changes, old_name, new_name
        )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nSchema evolution analysis complete:")
    print(f"  Verdict: {report.get('overall_verdict', 'N/A')}")
    print(f"  Total changes: {report.get('total_changes', 0)}")
    if report.get("breaking_changes", 0) > 0:
        print(f"  BREAKING changes: {report['breaking_changes']}")
    print(f"  Report written to {args.output}")


if __name__ == "__main__":
    main()
