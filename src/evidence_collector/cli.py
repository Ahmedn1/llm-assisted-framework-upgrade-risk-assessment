import argparse
import json
from pathlib import Path
from .collector import collect
from .transport import CollectionHttpClient


def main(argv=None):
    parser = argparse.ArgumentParser(description='Collect upgrade evidence from a resolver JSON result without an LLM')
    parser.add_argument('--resolution', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cache-dir', type=Path, default=Path('.cache/upgrade-resolver'))
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--max-documents', type=int, default=100)
    parser.add_argument('--max-requests', type=int, default=200)
    parser.add_argument('--max-depth', type=int, default=2)
    parser.add_argument('--max-urls', type=int, default=500)
    parser.add_argument('--max-index-pages', type=int, default=8)
    parser.add_argument('--search', choices=('none', 'auto', 'brave', 'duckduckgo'), default='none')
    parser.add_argument('--source-url', action='append', default=[])
    args = parser.parse_args(argv)
    if args.offline and args.refresh:
        parser.error('--offline and --refresh cannot be combined')
    if args.max_requests < 1:
        parser.error('--max-requests must be positive')
    try:
        resolution = json.loads(args.resolution.read_text())
        if isinstance(resolution, dict) and isinstance(resolution.get('result'), dict):
            resolution = resolution['result']  # Local-model runner report envelope.
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    client = CollectionHttpClient(args.cache_dir, offline=args.offline, refresh=args.refresh,
                                  max_requests=args.max_requests, max_bytes=10_000_000)
    try:
        result = collect(client, resolution, max_documents=args.max_documents, max_depth=args.max_depth,
                         max_urls=args.max_urls, max_index_pages=args.max_index_pages,
                         search=args.search, extra_urls=args.source_url)
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    finally:
        client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"Collected {len(result['documents'])} documents; {len(result['failures'])} retrieval failures; coverage remains partial")
    return 0 if result['documents'] else 2
