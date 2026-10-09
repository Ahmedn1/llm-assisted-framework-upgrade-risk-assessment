"""Serial, cached retrieval with bounded backoff; no bot-challenge bypass."""
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from upgrade_resolver.http import FetchError, HttpClient


class CollectionHttpClient(HttpClient):
    def __init__(self, *args, min_interval=0.25, retries=2, **kwargs):
        super().__init__(*args, **kwargs)
        self.min_interval, self.retries = min_interval, retries
        self.last_host_request = {}
        self.deferred_hosts = set()
        self.forbidden_counts = {}

    def get(self, url, *, accept='application/json'):
        host = urlsplit(url).hostname
        if host in self.deferred_hosts:
            # Cached documents remain usable even when live requests are deferred.
            previous = self.offline
            self.offline = True
            try:
                return super().get(url, accept=accept)
            except FetchError as exc:
                raise FetchError(f'Host deferred for this run: {host}') from exc
            finally:
                self.offline = previous
        for attempt in range(self.retries + 1):
            if not self.offline:
                delay = self.min_interval - (time.monotonic() - self.last_host_request.get(host, 0))
                if delay > 0:
                    time.sleep(delay)
                self.last_host_request[host] = time.monotonic()
            self.last_response_headers = {}
            try:
                return super().get(url, accept=accept)
            except FetchError as exc:
                forbidden = 'HTTP 403 ' in str(exc)
                limited = 'HTTP 429 ' in str(exc) or (forbidden and (
                    self.last_response_headers.get('x-ratelimit-remaining') == '0' or
                    'retry-after' in self.last_response_headers))
                if forbidden:
                    self.forbidden_counts[host] = self.forbidden_counts.get(host, 0) + 1
                if forbidden and (limited or self.forbidden_counts[host] >= 3):
                    self.deferred_hosts.add(host)
                    raise
                if limited and attempt == self.retries:
                    self.deferred_hosts.add(host)
                transient = any(f'HTTP {code} ' in str(exc) for code in (429, 500, 502, 503, 504))
                if self.offline or not transient or attempt == self.retries:
                    raise
                wait = float(2 ** attempt)
                retry = self.last_response_headers.get('retry-after')
                if retry:
                    try:
                        wait = max(wait, float(retry))
                    except ValueError:
                        try:
                            wait = max(wait, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                if wait > 10:
                    self.deferred_hosts.add(host)
                    raise FetchError(f'Host requested a retry delay of {wait:.0f}s; deferred instead of retrying early: {url}') from exc
                time.sleep(wait)
