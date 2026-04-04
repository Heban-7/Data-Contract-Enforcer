"""ReportGenerator — produces the Enforcer Report from live validation data.

Auto-generates enforcer_report/report_data.json with five sections:
  1. Data Health Score (0-100)
  2. Violations this week (by severity, plain-language top 3)
  3. Schema changes detected
  4. AI system risk assessment
  5. Recommended actions

Usage:
    python contracts/report_generator.py
"""

import json
import glob
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Health score computation
# ---------------------------------------------------------------------------

SEVERITY_DEDUCTIONS = {
    "CRITICAL": 20,
    "HIGH": 10,
    "MEDIUM": 5,
    "LOW": 1,
}


def compute_health_score(validation_reports: list[dict]) -> int:
    """0-100 score. Start at 100, subtract per violation severity."""
    score = 100
    for report in validation_reports:
        for result in report.get("results", []):
            if result["status"] in ("FAIL", "ERROR"):
                severity = result.get("severity", "LOW")
                score -= SEVERITY_DEDUCTIONS.get(severity, 1)
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Plain language formatting
# ---------------------------------------------------------------------------

def plain_language_violation(result: dict) -> str:
    """Convert a technical check result into plain language."""
    col = result.get("column_name", "unknown field")
    system = result.get("check_id", "").split(".")[0]
    check_type = result.get("check_type", "check")
    expected = result.get("expected", "N/A")
    actual = result.get("actual_value", "N/A")
    records = result.get("records_failing", "unknown")

    return (
        f"The {col} field in {system} failed its {check_type} check. "
        f"Expected {expected} but found {actual}. "
        f"This affects {records} records."
    )


def health_narrative(score: int, critical_count: int) -> str:
    """Generate a one-sentence health narrative."""
    if score >= 90:
        return f"Data health score of {score}/100. No critical violations detected."
    elif score >= 70:
        return (
            f"Data health score of {score}/100. "
            f"{critical_count} issue(s) require attention but overall quality is acceptable."
        )
    elif score >= 50:
        return (
            f"Data health score of {score}/100. "
            f"{critical_count} critical issue(s) require immediate action. "
            "Data quality is degraded."
        )
    else:
        return (
            f"Data health score of {score}/100. "
            f"System is in critical state with {critical_count} severe violations. "
            "Immediate intervention required."
        )


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def load_validation_reports(reports_dir: str = "validation_reports/") -> list[dict]:
    reports = []
    for p in glob.glob(f"{reports_dir}*.json"):
        name = Path(p).name
        if name in ("ai_extensions.json", "ai_metrics.json",
                     "schema_evolution_week3.json", "schema_evolution.json"):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if "results" in data:
                reports.append(data)
        except (json.JSONDecodeError, KeyError):
            continue
    return reports


def load_violations(violations_dir: str = "violation_log/") -> list[dict]:
    vlog = Path(violations_dir) / "violations.jsonl"
    if not vlog.exists():
        return []
    with open(vlog, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def load_schema_evolution(reports_dir: str = "validation_reports/") -> list[dict]:
    changes = []
    for p in glob.glob(f"{reports_dir}schema_evolution*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            changes.append(data)
        except (json.JSONDecodeError, KeyError):
            continue
    return changes


def load_ai_metrics(path: str = "validation_reports/ai_metrics.json") -> dict:
    if Path(path).exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def resolve_contract_path(check_id: str) -> str:
    contract_id = check_id.split(".", 1)[0]
    expected = Path("generated_contracts") / f"{contract_id.replace('-', '_')}.yaml"
    if expected.exists():
        return str(expected)
    return f"generated_contracts/{contract_id.replace('-', '_')}.yaml"


def derive_clause_identifier(result: dict) -> str:
    check_id = result.get("check_id", "")
    parts = check_id.split(".")
    if len(parts) >= 3:
        return f"{parts[1]}.{parts[2]}"
    col = result.get("column_name", "unknown")
    ctype = result.get("check_type", "check")
    return f"{col}.{ctype}"


def build_action_from_failure(
    failure: dict, violations_by_check_id: dict[str, list[dict]]
) -> str:
    check_id = failure.get("check_id", "unknown.check")
    contract_file = resolve_contract_path(check_id)
    clause_id = derive_clause_identifier(failure)
    detail = failure.get("message", "resolve the failing check")

    attribution_note = ""
    linked_violations = violations_by_check_id.get(check_id, [])
    if linked_violations:
        v = linked_violations[0]
        blame_chain = v.get("blame_chain", [])
        top_file = blame_chain[0].get("file_path") if blame_chain else "upstream source"
        depth = v.get("blast_radius", {}).get("max_contamination_depth")
        if depth is not None:
            attribution_note = (
                f" Probable source: {top_file}; contamination depth={depth}."
            )
        else:
            attribution_note = f" Probable source: {top_file}."

    return (
        f"Fix `{check_id}` by updating upstream producer logic; align with "
        f"`{contract_file}` clause `{clause_id}`. {detail}.{attribution_note}"
    )


def generate_recommendations(all_failures: list[dict],
                              schema_changes: list[dict],
                              violations: list[dict]) -> list[str]:
    """Generate 3 prioritized, specific recommendations."""
    recommendations = []
    violations_by_check_id: dict[str, list[dict]] = {}
    for v in violations:
        cid = v.get("check_id", "")
        if cid:
            violations_by_check_id.setdefault(cid, []).append(v)

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "WARNING": 4}
    ranked_failures = sorted(
        all_failures,
        key=lambda f: severity_order.get(f.get("severity", "LOW"), 99),
    )

    for f in ranked_failures[:3]:
        recommendations.append(build_action_from_failure(f, violations_by_check_id))

    for sc in schema_changes:
        if sc.get("overall_verdict") == "BREAKING":
            contract_id = sc.get("contract_id", "unknown")
            contract_file = f"generated_contracts/{contract_id.replace('-', '_')}.yaml"
            recommendations.append(
                f"Review breaking schema evolution in `{contract_file}` "
                f"(contract `{contract_id}`) and execute the migration checklist "
                "before deployment."
            )
            break

    if not recommendations:
        recommendations = [
            "All checks passing. Continue monitoring for drift.",
            "Add contract enforcement step to CI/CD pipeline.",
            "Review statistical baselines quarterly for recalibration.",
        ]

    while len(recommendations) < 3:
        defaults = [
            "Add contract enforcement step to CI/CD pipeline.",
            "Review statistical baselines quarterly for recalibration.",
            "Expand contract coverage to remaining inter-system interfaces.",
        ]
        for d in defaults:
            if d not in recommendations:
                recommendations.append(d)
                break
        else:
            break

    return recommendations[:3]


def generate_report(
    reports_dir: str = "validation_reports/",
    violations_dir: str = "violation_log/",
) -> dict:
    reports = load_validation_reports(reports_dir)
    violations = load_violations(violations_dir)
    schema_changes = load_schema_evolution(reports_dir)
    ai_metrics = load_ai_metrics()

    health_score = compute_health_score(reports)

    all_failures = [
        r for rep in reports
        for r in rep.get("results", [])
        if r["status"] in ("FAIL", "ERROR")
    ]

    severity_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    sorted_failures = sorted(
        all_failures,
        key=lambda x: severity_order.index(x.get("severity", "LOW"))
        if x.get("severity", "LOW") in severity_order else 99,
    )
    top_3 = sorted_failures[:3]

    critical_count = sum(
        1 for f in all_failures if f.get("severity") == "CRITICAL"
    )

    schema_summary = []
    for sc in schema_changes:
        schema_summary.append({
            "contract_id": sc.get("contract_id", "unknown"),
            "verdict": sc.get("overall_verdict", "N/A"),
            "total_changes": sc.get("total_changes", 0),
            "breaking_changes": sc.get("breaking_changes", 0),
        })

    ai_risk = {}
    metrics = ai_metrics.get("metrics", {})
    for check_name, data in metrics.items():
        ai_risk[check_name] = {
            "status": data.get("status", "unknown"),
        }
        if "drift_score" in data:
            ai_risk[check_name]["drift_score"] = data["drift_score"]
        if "violation_rate" in data:
            ai_risk[check_name]["violation_rate"] = data["violation_rate"]

    recommendations = generate_recommendations(
        all_failures, schema_changes, violations
    )

    now = datetime.now(timezone.utc)
    report = {
        "generated_at": now.isoformat(),
        "period": f"{(now - timedelta(days=7)).date()} to {now.date()}",
        "data_health_score": health_score,
        "health_narrative": health_narrative(health_score, critical_count),
        "violations_summary": {
            "total": len(all_failures),
            "by_severity": {
                sev: sum(1 for f in all_failures if f.get("severity") == sev)
                for sev in severity_order
            },
            "top_violations": [plain_language_violation(v) for v in top_3],
        },
        "schema_changes": schema_summary,
        "ai_system_risk_assessment": ai_risk if ai_risk else {
            "status": "No AI metrics available. Run ai_extensions.py first.",
        },
        "violation_log_count": len(violations),
        "recommendations": recommendations,
        "validation_reports_analyzed": len(reports),
    }

    return report


def main():
    print("Generating Enforcer Report...")

    report = generate_report()

    output_dir = Path("enforcer_report")
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / "report_data.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nEnforcer Report generated:")
    print(f"  Data Health Score: {report['data_health_score']}/100")
    print(f"  {report['health_narrative']}")
    print(f"  Total violations: {report['violations_summary']['total']}")
    print(f"  Schema changes: {len(report['schema_changes'])}")
    print(f"  Recommendations: {len(report['recommendations'])}")
    print(f"  Written to {report_path}")


if __name__ == "__main__":
    main()
