"""Run the example inputs through the same generic resolver as the CLI."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from upgrade_resolver.http import HttpClient
from upgrade_resolver.resolver import resolve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/resolver-examples"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/upgrade-resolver"))
    args = parser.parse_args()
    inputs = json.loads(Path(__file__).with_name("inputs.json").read_text())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for item in inputs:
        client = HttpClient(args.cache_dir, offline=args.offline)
        try:
            result = resolve(client, item["project"], item["current_version"], item["target_version"])
        finally:
            client.close()
        filename = item["project"].lower() + ".json"
        (args.output_dir / filename).write_text(json.dumps(result, indent=2) + "\n")
        row = {**item, "status": result["status"], "identity": result["identity"]["id"] if result["identity"] else None,
               "selected_catalog": result.get("selected_catalog"), "version_count": len(result["versions"]),
               "documentation_milestones": result["documentation_milestones"], "coverage": result["coverage"],
               "network_requests": result["network_requests"], "llm_calls": result["llm_calls"], "output_file": filename}
        results.append(row)
        print(json.dumps(row), flush=True)
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "offline_replay": args.offline,
               "source_urls_supplied": False, "results": results}
    (args.output_dir / "validation-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if all(r["status"] == "resolved" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
