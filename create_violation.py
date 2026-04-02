"""Inject known violations into test data for contract enforcement testing.

Injection A: Scale confidence from 0.0-1.0 to 0-100 in Week 3 extractions
Injection B: Add invalid entity type 'INSTITUTION' to a Week 3 record
"""

import json
import random

random.seed(99)


def inject_scale_violation():
    """Change confidence from 0.0-1.0 float to 0-100 integer percentage."""
    records = []
    with open("outputs/week3/extractions.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            for fact in r.get("extracted_facts", []):
                fact["confidence"] = round(fact["confidence"] * 100, 1)
            records.append(r)

    with open("outputs/week3/extractions_violated.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    print(f"INJECTION A: confidence scale changed from 0.0-1.0 to 0-100 "
          f"in {len(records)} records")
    return "outputs/week3/extractions_violated.jsonl"


def inject_enum_violation():
    """Add an invalid entity type to a Week 3 record."""
    records = []
    with open("outputs/week3/extractions.jsonl", encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))

    modified = 0
    for r in records[:3]:
        for entity in r.get("entities", []):
            if entity.get("type") == "ORG":
                entity["type"] = "INSTITUTION"
                modified += 1
                break

    with open("outputs/week3/extractions_enum_violated.jsonl", "w",
              encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    print(f"INJECTION B: Changed {modified} entity types from 'ORG' to "
          f"'INSTITUTION' (not in enum)")
    return "outputs/week3/extractions_enum_violated.jsonl"


if __name__ == "__main__":
    print("Creating violation test data...\n")
    inject_scale_violation()
    inject_enum_violation()
    print("\nDone. Violated files written to outputs/week3/")
