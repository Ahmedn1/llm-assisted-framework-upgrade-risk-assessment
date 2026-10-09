"""Shared bounded web search with explicit provider and fallback provenance."""
from dataclasses import dataclass, field
import os
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from upgrade_resolver.http import FetchError


@dataclass
class SearchResult:
    url: str
    title: str = ""
    description: str = ""


@dataclass
class SearchResponse:
    provider: str
    source_url: str
    results: list[SearchResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def search_web(client, query: str, *, provider="auto", limit=5) -> SearchResponse:
    """Prefer configured Brave; fall back on unavailable/invalid Brave responses.

    The injected HTTP client supplies cache, offline mode, credentials and budgets.
    Explicit duckduckgo bypasses Brave; none performs no retrieval. An empty valid
    Brave result is not a provider failure. Bot challenges are reported, not bypassed.
    """
    if provider not in ("auto", "brave", "duckduckgo", "none"):
        raise ValueError("Unknown search provider")
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Supply a nonempty query and a limit between 1 and 20")
    if provider == "none":
        return SearchResponse("none", "")
    warnings = []
    if provider in ("auto", "brave"):
        if os.getenv("BRAVE_SEARCH_API_KEY") or (provider == "brave" and client.offline):
            url = "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": limit})
            try:
                data = client.json(url)
                if not isinstance(data, dict) or "error" in data:
                    raise ValueError("Invalid Brave response")
                web = data.get("web", {})
                if not isinstance(web, dict) or not isinstance(web.get("results", []), list):
                    raise ValueError("Invalid Brave results")
                results = []
                for item in web.get("results", [])[:limit]:
                    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                        raise ValueError("Invalid Brave result URL")
                    results.append(SearchResult(item["url"], item.get("title") or "", item.get("description") or ""))
                return SearchResponse("brave", url, results)
            except (FetchError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f"Brave unavailable; falling back to DuckDuckGo: {exc}")
        elif provider == "brave":
            warnings.append("BRAVE_SEARCH_API_KEY is not set; falling back to DuckDuckGo")
    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    try:
        page = client.get(url, accept="text/html")
        soup = BeautifulSoup(page["body"], "html.parser")
        if page["status"] == 202 or soup.select("#challenge-form, #anomaly-modal") or "anomaly.js" in page["body"]:
            raise FetchError("Search provider returned a bot challenge")
        results = []
        for anchor in soup.select("a.result__a")[:limit]:
            href = urljoin(url, anchor.get("href", ""))
            link = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
            results.append(SearchResult(link, anchor.get_text(" ", strip=True)))
        return SearchResponse("duckduckgo", url, results, warnings)
    except (FetchError, ValueError, KeyError, TypeError) as exc:
        raise FetchError("; ".join(warnings + [f"DuckDuckGo unavailable: {exc}"])) from exc
