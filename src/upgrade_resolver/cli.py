import argparse
from contextlib import ExitStack
import json
import sys
from pathlib import Path

from .http import FetchError, HttpClient
from .resolver import resolve
from .versions import validate_interval


def positive_int(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Discover a software project's release catalog and enumerate an upgrade interval with optional decision scoring.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--source-url", action="append", default=[], help="Optional discovery hint; may be repeated")
    parser.add_argument("--version-mode", choices=("family", "exact"), default="family")
    parser.add_argument("--web-search", choices=("auto", "none", "duckduckgo", "brave"), default="auto")
    parser.add_argument("--scorer", choices=("heuristic", "decision"), default="heuristic",
                        help="decision scores sources with the scorer decision model (default local d1; DECISION_SCORER_* / DECISION_*)")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/upgrade-resolver"))
    parser.add_argument("--offline", action="store_true", help="Replay cached responses without network access")
    parser.add_argument("--refresh", action="store_true", help="Replace cached snapshots with live responses")
    parser.add_argument("--max-requests", type=positive_int, default=100)
    parser.add_argument("--max-pages", type=positive_int, default=30, help="Maximum pages per GitHub catalog")
    parser.add_argument("--max-html-pages", type=positive_int, default=8)
    parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout")
    # Same scorer-stage settings as the pipeline; upgrade_pipeline.models has no model dependencies at import.
    from upgrade_pipeline.models import ModelRegistry, add_model_arguments, resolve_decision
    add_model_arguments(parser, decision_stages=("scorer",))
    args = parser.parse_args(argv)
    if args.offline and args.refresh:
        parser.error("--offline and --refresh cannot be used together")
    try:
        validate_interval(args.current_version, args.target_version, args.version_mode)
        if not args.project.strip() or len(args.project) > 150:
            raise ValueError("Project name must contain 1–150 characters.")
    except ValueError as exc:
        parser.error(str(exc))
    client = HttpClient(args.cache_dir, offline=args.offline, refresh=args.refresh, max_requests=args.max_requests)
    stack = ExitStack()
    try:
        backend = None
        if args.scorer == "decision":
            settings = resolve_decision("scorer", args)
            backend = ModelRegistry(stack, offline=args.offline).decision(settings)
            described = settings.describe()
            print(f"model: scorer: {described['kind']} {described['model']}"
                  + (f"@{described['revision'][:12]}" if described["revision"] else "")
                  + (f" at {described['base_url']}" if described["base_url"] else ""), file=sys.stderr)
        result = resolve(client, args.project, args.current_version, args.target_version,
                         seed_urls=args.source_url, mode=args.version_mode, web_search=args.web_search,
                         max_pages=args.max_pages, max_html_pages=args.max_html_pages,
                         scorer=args.scorer, decision_backend=backend)
    except (ValueError, FetchError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        stack.close()
        client.close()
    serialized = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized)
    else:
        print(serialized, end="")
    print(f"{result['status']}: {len(result['versions'])} versions; milestones: {', '.join(result['documentation_milestones']) or 'none'}", file=sys.stderr)
    return 0 if result["status"] == "resolved" else 2


if __name__ == "__main__":
    sys.exit(main())
