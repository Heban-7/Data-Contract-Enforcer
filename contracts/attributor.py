"""ViolationAttributor — traces contract violations to their upstream cause.

Uses the Week 4 lineage graph and git history to build a blame chain
for each FAIL result in a validation report.

Usage:
    python contracts/attributor.py \
        --violation validation_reports/violated_run.json \
        --lineage outputs/week4/lineage_snapshots.jsonl \
        --contract generated_contracts/week3_document_refinery_extractions.yaml \
        --output violation_log/violations.jsonl
"""

import argparse
import json
import subprocess
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Lineage graph traversal
# ---------------------------------------------------------------------------

def load_lineage(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    if not lines:
        raise ValueError(f"No lineage snapshots found in {path}")
    return json.loads(lines[-1])


def load_subscriptions_registry(path: str) -> list[dict]:
    """Load producer->consumer subscriptions with field-level dependencies."""
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("subscriptions", [])
    if isinstance(data, list):
        return data
    return []


def source_system_from_check_id(check_id: str) -> str:
    # week3-document-refinery-extractions.field.range -> week3
    root = check_id.split(".", 1)[0]
    return root.split("-", 1)[0]


def field_from_check_id(check_id: str) -> str:
    parts = check_id.split(".")
    if len(parts) >= 2:
        return parts[1]
    return ""


def find_upstream_files(failing_check_id: str, lineage_snapshot: dict) -> list[dict]:
    """BFS upstream from the failing schema element to find FILE nodes.

    Parse the check_id to determine the system (e.g. 'week3') and search
    for nodes whose node_id contains that system identifier.
    """
    parts = failing_check_id.split(".")
    system_key = parts[0] if parts else ""

    adjacency_reverse: dict[str, list[str]] = {}
    for edge in lineage_snapshot.get("edges", []):
        tgt = edge["target"]
        src = edge["source"]
        adjacency_reverse.setdefault(tgt, []).append(src)

    start_nodes = []
    node_map = {n["node_id"]: n for n in lineage_snapshot.get("nodes", [])}
    for node_id, node in node_map.items():
        if system_key in node_id and node["type"] != "FILE":
            start_nodes.append(node_id)
    if not start_nodes:
        for node_id, node in node_map.items():
            if system_key in node_id:
                start_nodes.append(node_id)

    visited = set()
    file_candidates = []
    queue = deque()
    for sn in start_nodes:
        queue.append((sn, 0))
        visited.add(sn)

    while queue:
        current, distance = queue.popleft()
        if distance > 5:
            continue
        node = node_map.get(current)
        if node and node["type"] == "FILE":
            file_candidates.append({
                "node_id": current,
                "path": node.get("metadata", {}).get("path", current),
                "distance": distance,
            })
        for neighbor in adjacency_reverse.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, distance + 1))

    return file_candidates


# ---------------------------------------------------------------------------
# Git blame integration
# ---------------------------------------------------------------------------

def get_recent_commits(file_path: str, days: int = 14,
                       repo_root: str = ".") -> list[dict]:
    """Run git log on a file and parse structured output."""
    cmd = [
        "git", "log", "--follow",
        f"--since={days} days ago",
        "--format=%H|%ae|%ai|%s",
        "--", file_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=repo_root, timeout=10
        )
        commits = []
        for line in result.stdout.strip().split("\n"):
            if "|" in line:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    commits.append({
                        "commit_hash": parts[0],
                        "author": parts[1],
                        "commit_timestamp": parts[2].strip(),
                        "commit_message": parts[3],
                    })
        return commits
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def get_all_recent_commits(repo_root: str = ".", days: int = 14) -> list[dict]:
    """Fallback: get all recent commits from the repo."""
    cmd = [
        "git", "log",
        f"--since={days} days ago",
        "--format=%H|%ae|%ai|%s",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=repo_root, timeout=10
        )
        commits = []
        for line in result.stdout.strip().split("\n"):
            if "|" in line:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    commits.append({
                        "commit_hash": parts[0],
                        "author": parts[1],
                        "commit_timestamp": parts[2].strip(),
                        "commit_message": parts[3],
                    })
        return commits
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def score_candidates(commits: list[dict], violation_timestamp: str,
                     lineage_distance: int) -> list[dict]:
    """Score and rank blame candidates by recency and lineage proximity."""
    try:
        v_time = datetime.fromisoformat(
            violation_timestamp.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        v_time = datetime.now(timezone.utc)

    scored = []
    for commit in commits[:5]:
        try:
            ts_str = commit["commit_timestamp"]
            ts_str = ts_str.replace(" +", "+").replace(" -", "-")
            if " " in ts_str and ("+" not in ts_str and "-" not in ts_str[11:]):
                ts_str = ts_str + "+00:00"
            c_time = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            c_time = v_time

        days_diff = abs((v_time - c_time).days)
        score = max(0.0, 1.0 - (days_diff * 0.1) - (lineage_distance * 0.2))
        scored.append({
            **commit,
            "confidence_score": round(score, 3),
        })
    scored = sorted(scored, key=lambda x: x["confidence_score"], reverse=True)
    for i, item in enumerate(scored, start=1):
        item["rank"] = i
    return scored


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------

def compute_contamination_depth(
    source_system: str, subscriptions: list[dict]
) -> dict[str, int]:
    """BFS over subscriptions graph to estimate dependency-distance contamination."""
    adjacency: dict[str, list[str]] = {}
    for sub in subscriptions:
        upstream = sub.get("upstream_system")
        downstream = sub.get("downstream_system")
        if upstream and downstream:
            adjacency.setdefault(upstream, []).append(downstream)

    depths: dict[str, int] = {}
    queue = deque([(source_system, 0)])
    visited = {source_system}
    while queue:
        current, depth = queue.popleft()
        for nxt in adjacency.get(current, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            depths[nxt] = depth + 1
            queue.append((nxt, depth + 1))
    return depths


def compute_blast_radius(
    contract_path: str,
    records_failing: int,
    check_id: str,
    subscriptions: list[dict],
) -> dict:
    """Compute blast radius from subscriptions registry (fallback to contract lineage)."""
    source_system = source_system_from_check_id(check_id)
    failing_field = field_from_check_id(check_id)
    contamination_depth = compute_contamination_depth(source_system, subscriptions)

    affected_links = []
    affected_nodes: set[str] = set()
    for sub in subscriptions:
        if sub.get("upstream_system") != source_system:
            continue
        breaking_fields = sub.get("breaking_fields", [])
        fields_consumed = sub.get("fields_consumed", [])
        if breaking_fields and failing_field not in breaking_fields and "*" not in breaking_fields:
            continue
        downstream = sub.get("downstream_system", "")
        if downstream:
            affected_nodes.add(downstream)
        affected_links.append(
            {
                "subscription_id": sub.get("subscription_id"),
                "upstream_system": source_system,
                "downstream_system": downstream,
                "fields_consumed": fields_consumed,
                "breaking_fields": breaking_fields,
                "contamination_depth": contamination_depth.get(downstream, 1),
            }
        )

    # Fallback for older contracts that don't yet have a subscriptions file.
    if not affected_nodes:
        try:
            with open(contract_path, encoding="utf-8") as f:
                contract = yaml.safe_load(f)
            downstream = contract.get("lineage", {}).get("downstream", [])
            for item in downstream:
                node_id = item.get("id", "")
                if node_id:
                    affected_nodes.add(node_id)
        except (FileNotFoundError, yaml.YAMLError):
            pass

    affected_pipelines = [n for n in affected_nodes if "pipeline" in n.lower()]
    max_depth = max(contamination_depth.values()) if contamination_depth else 0
    return {
        "affected_nodes": sorted(affected_nodes),
        "affected_pipelines": sorted(affected_pipelines),
        "affected_links": affected_links,
        "contamination_depth": contamination_depth,
        "max_contamination_depth": max_depth,
        "estimated_records": records_failing,
    }


# ---------------------------------------------------------------------------
# Main attribution pipeline
# ---------------------------------------------------------------------------

def attribute_violations(violation_report_path: str, lineage_path: str,
                         contract_path: str,
                         subscriptions_path: str) -> list[dict]:
    with open(violation_report_path, encoding="utf-8") as f:
        report = json.load(f)

    subscriptions = load_subscriptions_registry(subscriptions_path)
    lineage = load_lineage(lineage_path)

    failures = [
        r for r in report.get("results", [])
        if r["status"] in ("FAIL", "ERROR")
    ]

    if not failures:
        print("  No failures found in validation report.")
        return []

    violation_records = []
    for failure in failures:
        check_id = failure["check_id"]
        detection_time = report.get("run_timestamp",
                                     datetime.now(timezone.utc).isoformat())

        upstream_files = find_upstream_files(check_id, lineage)

        all_commits = []
        min_distance = 1
        for uf in upstream_files:
            commits = get_recent_commits(uf["path"])
            for c in commits:
                c["_file_path"] = uf["path"]
                c["_distance"] = uf["distance"]
            all_commits.extend(commits)
            if uf["distance"] > 0:
                min_distance = min(min_distance, uf["distance"])

        if not all_commits:
            all_commits = get_all_recent_commits()
            for c in all_commits:
                c["_file_path"] = "repository-wide"
                c["_distance"] = 1

        scored = score_candidates(all_commits, detection_time, min_distance)

        blame_chain = []
        for entry in scored[:5]:
            blame_chain.append({
                "rank": entry["rank"],
                "file_path": entry.get("_file_path", "unknown"),
                "commit_hash": entry["commit_hash"],
                "author": entry["author"],
                "commit_timestamp": entry["commit_timestamp"],
                "commit_message": entry["commit_message"],
                "confidence_score": entry["confidence_score"],
            })

        if not blame_chain:
            blame_chain.append({
                "rank": 1,
                "file_path": "unknown",
                "commit_hash": "0" * 40,
                "author": "unknown",
                "commit_timestamp": datetime.now(timezone.utc).isoformat(),
                "commit_message": "No recent commits found",
                "confidence_score": 0.0,
            })

        blast_radius = compute_blast_radius(
            contract_path=contract_path,
            records_failing=failure.get("records_failing", 0),
            check_id=check_id,
            subscriptions=subscriptions,
        )

        violation_records.append({
            "violation_id": str(uuid.uuid4()),
            "check_id": check_id,
            "detected_at": detection_time,
            "severity": failure.get("severity", "HIGH"),
            "message": failure.get("message", ""),
            "blame_chain": blame_chain,
            "blast_radius": blast_radius,
        })

    return violation_records


def main():
    parser = argparse.ArgumentParser(
        description="Attribute contract violations to upstream commits"
    )
    parser.add_argument("--violation", required=True,
                        help="Path to validation report JSON")
    parser.add_argument("--lineage", required=True,
                        help="Path to Week 4 lineage_snapshots.jsonl")
    parser.add_argument("--contract", required=True,
                        help="Path to contract YAML")
    parser.add_argument("--output", required=True,
                        help="Output path for violation log JSONL")
    parser.add_argument(
        "--subscriptions",
        default="contracts/subscriptions_registry.json",
        help="Path to subscriptions registry JSON",
    )
    args = parser.parse_args()

    print(f"Attributing violations...")
    print(f"  Report: {args.violation}")
    print(f"  Lineage: {args.lineage}")

    violations = attribute_violations(
        args.violation, args.lineage, args.contract, args.subscriptions
    )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    with open(args.output, "a", encoding="utf-8") as f:
        for v in violations:
            f.write(json.dumps(v, default=str) + "\n")

    print(f"\nAttribution complete:")
    print(f"  {len(violations)} violations attributed")
    print(f"  Written to {args.output}")
    for v in violations:
        print(f"    - {v['check_id']}: {len(v['blame_chain'])} candidates, "
              f"blast radius {len(v['blast_radius']['affected_nodes'])} nodes")


if __name__ == "__main__":
    main()
