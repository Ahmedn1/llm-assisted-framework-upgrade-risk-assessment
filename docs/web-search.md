# Shared web search

`src/web_search/__init__.py` exposes one high-level entry point:

```python
from web_search import search_web

response = search_web(client, '"React" official releases 17 to 18')
for result in response.results:
    print(result.url, result.title)
```

The injected project HTTP client owns credentials, public URL checks, caching,
offline behavior, request limits, and response provenance. Set
`BRAVE_SEARCH_API_KEY` in the environment to enable Brave. No credentials are
included in search URLs.

- `auto` prefers Brave when configured, otherwise DuckDuckGo HTML.
- `brave` prefers Brave but also falls back if the key is missing or the API fails.
  Explicit offline Brave can read cached results without a key.
- `duckduckgo` bypasses Brave; `none` performs no search.
- HTTP/access errors, timeouts, and malformed Brave results trigger fallback.
  Valid empty results do not.
- DuckDuckGo bot challenges raise `FetchError`; they are not bypassed. If both
  providers fail, the error preserves both causes.

The response contains normalized results, the actual provider, search URL, and
fallback warnings. Default result limit is five. Resolver discovery and optional
collector search use this same entry point; package-registry and repository API
queries remain separate because they are not general web search.

Collector flags: `upgrade-collect --search auto`; pipeline flag:
`--evidence-search auto`. Evidence search remains disabled by default. Collector
reports distinguish `search_requested` from the actual `search_provider`.
