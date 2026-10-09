"""Bounded public HTTP retrieval with auditable, replayable response snapshots."""
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx


class FetchError(RuntimeError):
    pass


def validate_public_url(url: str, resolve_dns: bool = True) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise FetchError("Only public HTTP(S) URLs without credentials are accepted.")
    if parsed.port not in (None, 80, 443):
        raise FetchError("Only standard HTTP(S) ports are accepted.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise FetchError("Local URLs are not accepted.")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = []
        if resolve_dns:
            try:
                addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or 443)]
            except OSError as exc:
                raise FetchError(f"DNS lookup failed for {host}: {exc}") from exc
    if any(not address.is_global for address in addresses):
        raise FetchError("Non-public network addresses are not accepted.")


class HttpClient:
    def __init__(self, cache_dir: Path, *, offline: bool = False, refresh: bool = False,
                 timeout: float = 15, max_requests: int = 100, max_bytes: int = 50_000_000):
        self.cache_dir = cache_dir
        self.offline = offline
        self.refresh = refresh
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.request_count = 0
        self.sources: dict[str, dict] = {}
        self.last_response_headers = {}
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": "UpgradeResolver/0.1 (public release catalog research)"})

    def close(self):
        self.client.close()

    def get(self, url: str, *, accept: str = "application/json") -> dict:
        validate_public_url(url, resolve_dns=False)
        key = hashlib.sha256((url + "\n" + accept).encode()).hexdigest()
        path = self.cache_dir / (key + ".json")
        cached_snapshot = None
        if path.exists():
            try:
                snapshot = json.loads(path.read_text())
                if snapshot["url"] != url or hashlib.sha256(snapshot["body"].encode()).hexdigest() != snapshot["content_sha256"]:
                    raise ValueError("snapshot integrity check failed")
            except (ValueError, KeyError, OSError) as exc:
                raise FetchError(f"Invalid cache entry for {url}: {exc}") from exc
            cached_snapshot = snapshot
            if self.offline or not self.refresh:
                self._record(snapshot, True)
                if snapshot["status"] >= 400:
                    raise FetchError(f"Cached HTTP {snapshot['status']} for {url}")
                return snapshot
        if self.offline:
            raise FetchError(f"Offline cache miss: {url}")
        current = url
        for _ in range(6):
            if self.request_count >= self.max_requests:
                raise FetchError(f"Request budget ({self.max_requests}) exhausted")
            validate_public_url(current)
            headers = {"Accept": accept}
            if cached_snapshot and current == cached_snapshot.get("final_url", url) and cached_snapshot['status'] == 200:
                validators = cached_snapshot.get('headers', {})
                if validators.get('etag'):
                    headers['If-None-Match'] = validators['etag']
                elif validators.get('last-modified'):
                    headers['If-Modified-Since'] = validators['last-modified']
            # Never forward provider credentials to arbitrary URLs or redirect hosts.
            host = urlsplit(current).hostname
            if host == "api.github.com" and os.getenv("GITHUB_TOKEN"):
                headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
            if host == "api.search.brave.com" and os.getenv("BRAVE_SEARCH_API_KEY"):
                headers["X-Subscription-Token"] = os.environ["BRAVE_SEARCH_API_KEY"]
            self.request_count += 1
            try:
                with self.client.stream("GET", current, headers=headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = urljoin(current, response.headers["location"])
                        continue
                    self.last_response_headers = {k: response.headers[k] for k in ('retry-after', 'x-ratelimit-reset', 'x-ratelimit-remaining') if k in response.headers}
                    if response.status_code == 304 and cached_snapshot:
                        snapshot = {**cached_snapshot, 'validated_at': datetime.now(timezone.utc).isoformat()}
                        self._record(snapshot, True)
                        path.write_text(json.dumps(snapshot))
                        return snapshot
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise FetchError(f"Response exceeds {self.max_bytes} bytes: {url}")
                    body = data.decode("utf-8", errors="replace")
                    snapshot = {
                        "url": url, "final_url": current, "status": response.status_code,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "headers": {k: response.headers[k] for k in ("link", "content-type", "retry-after", "x-ratelimit-remaining", "etag", "last-modified") if k in response.headers},
                        "body": body,
                    }
            except httpx.HTTPError as exc:
                raise FetchError(f"HTTP request failed for {url}: {type(exc).__name__}") from exc
            self._record(snapshot, False)
            # Cache successes and stable misses, not rate limits or server failures.
            if snapshot["status"] < 400 or snapshot["status"] in (404, 410):
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", dir=self.cache_dir, delete=False) as file:
                    json.dump(snapshot, file)
                    temporary = file.name
                os.replace(temporary, path)
            if snapshot["status"] >= 400:
                raise FetchError(f"HTTP {snapshot['status']} for {url}")
            return snapshot
        raise FetchError(f"Too many redirects: {url}")

    def _record(self, snapshot: dict, cached: bool):
        self.sources[snapshot["url"]] = {k: v for k, v in snapshot.items() if k not in ("body", "headers")}
        self.sources[snapshot["url"]]["from_cache"] = cached
        self.sources[snapshot["url"]]["source_id"] = "source_" + hashlib.sha256(snapshot["url"].encode()).hexdigest()[:12]

    def json(self, url: str) -> dict | list:
        try:
            return json.loads(self.get(url)["body"])
        except (ValueError, TypeError) as exc:
            raise FetchError(f"Invalid JSON response: {url}") from exc
