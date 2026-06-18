#!/usr/bin/env python3
"""Compare two diagnostic metadata JSON files and print a human-readable diff."""
import argparse
import json
import sys
from pathlib import Path


def load_metadata(path: str) -> dict:
    """Load and validate a diagnostic metadata JSON file."""
    p = Path(path)
    if not p.exists():
        print(f"Error: {path} does not exist", file=sys.stderr)
        sys.exit(1)
    try:
        with open(p) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: {path} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def compare_metadata(old: dict, new: dict) -> dict:
    """Compare two metadata dicts and return diff results."""
    diff = {
        "added_modules": [],
        "removed_modules": [],
        "changed_status": [],
        "duration_deltas": [],
        "changed_commands": [],
        "changed_artifacts": [],
    }

    old_modules = {m["name"]: m for m in old.get("modules", [])}
    new_modules = {m["name"]: m for m in new.get("modules", [])}

    old_names = set(old_modules.keys())
    new_names = set(new_modules.keys())

    diff["added_modules"] = sorted(new_names - old_names)
    diff["removed_modules"] = sorted(old_names - new_names)

    for name in sorted(old_names & new_names):
        old_m = old_modules[name]
        new_m = new_modules[name]

        old_status = old_m.get("status")
        new_status = new_m.get("status")
        if old_status != new_status:
            diff["changed_status"].append({
                "module": name,
                "old": old_status,
                "new": new_status,
            })

        old_dur = old_m.get("duration_ms", 0)
        new_dur = new_m.get("duration_ms", 0)
        if old_dur != new_dur:
            diff["duration_deltas"].append({
                "module": name,
                "old_ms": old_dur,
                "new_ms": new_dur,
                "delta_ms": new_dur - old_dur,
            })

        old_cmd = old_m.get("command")
        new_cmd = new_m.get("command")
        if old_cmd != new_cmd:
            diff["changed_commands"].append({
                "module": name,
                "old": old_cmd,
                "new": new_cmd,
            })

        old_artifacts = set(old_m.get("artifacts", []))
        new_artifacts = set(new_m.get("artifacts", []))
        if old_artifacts != new_artifacts:
            diff["changed_artifacts"].append({
                "module": name,
                "added": sorted(new_artifacts - old_artifacts),
                "removed": sorted(old_artifacts - new_artifacts),
            })

    return diff


def print_human_diff(diff: dict, old_name: str, new_name: str):
    """Print a human-readable diff."""
    print(f"\nDiagnostic Diff: {old_name} → {new_name}")
    print("=" * 60)

    if diff["added_modules"]:
        print(f"\n✅ Added modules ({len(diff['added_modules'])}):")
        for m in diff["added_modules"]:
            print(f"  + {m}")

    if diff["removed_modules"]:
        print(f"\n❌ Removed modules ({len(diff['removed_modules'])}):")
        for m in diff["removed_modules"]:
            print(f"  - {m}")

    if diff["changed_status"]:
        print(f"\n🔄 Status changes ({len(diff['changed_status'])}):")
        for c in diff["changed_status"]:
            print(f"  {c['module']}: {c['old']} → {c['new']}")

    if diff["duration_deltas"]:
        print(f"\n⏱ Duration changes ({len(diff['duration_deltas'])}):")
        for d in diff["duration_deltas"]:
            sign = "+" if d["delta_ms"] >= 0 else ""
            print(f"  {d['module']}: {d['old_ms']}ms → {d['new_ms']}ms ({sign}{d['delta_ms']}ms)")

    if diff["changed_commands"]:
        print(f"\n🔧 Command changes ({len(diff['changed_commands'])}):")
        for c in diff["changed_commands"]:
            print(f"  {c['module']}:")
            print(f"    old: {c['old']}")
            print(f"    new: {c['new']}")

    if diff["changed_artifacts"]:
        print(f"\n📦 Artifact changes ({len(diff['changed_artifacts'])}):")
        for a in diff["changed_artifacts"]:
            print(f"  {a['module']}:")
            for added in a["added"]:
                print(f"    + {added}")
            for removed in a["removed"]:
                print(f"    - {removed}")

    if not any([diff["added_modules"], diff["removed_modules"], diff["changed_status"],
                diff["duration_deltas"], diff["changed_commands"], diff["changed_artifacts"]]):
        print("\n✅ No differences found")


def main():
    parser = argparse.ArgumentParser(description="Compare diagnostic metadata files")
    parser.add_argument("old", help="Path to old metadata JSON")
    parser.add_argument("new", help="Path to new metadata JSON")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    old = load_metadata(args.old)
    new = load_metadata(args.new)

    diff = compare_metadata(old, new)

    if args.json:
        print(json.dumps(diff, indent=2))
    else:
        print_human_diff(diff, args.old, args.new)

    has_changes = any([diff["added_modules"], diff["removed_modules"], diff["changed_status"],
                       diff["duration_deltas"], diff["changed_commands"], diff["changed_artifacts"]])
    sys.exit(0 if has_changes else 0)


if __name__ == "__main__":
    main()
