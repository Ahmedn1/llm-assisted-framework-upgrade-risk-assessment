import json
from upgrade_resolver.http import FetchError


class FakeClient:
    offline = False
    request_count = 0

    def __init__(self, handler):
        self.handler = handler
        self.sources = {}
        self.calls = []

    def get(self, url, *, accept="application/json"):
        self.calls.append((url, accept))
        value = self.handler(url, accept)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError("HTTP 404 for " + url)
        result = {"url": url, "final_url": url, "status": 200, "headers": {},
                  "body": value if isinstance(value, str) else json.dumps(value)}
        self.sources[url] = {"url": url, "status": 200}
        return result

    def json(self, url):
        return json.loads(self.get(url)["body"])
