"""Shared transport for explicitly configured inference endpoints, including localhost."""
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import math
import os
import time
from urllib.parse import urlsplit

import httpx


class ModelError(RuntimeError):
    """Base exception for inference failures."""


class ModelHTTPError(ModelError):
    def __init__(self, status_code: int, request_id: str | None = None):
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(f"Model endpoint returned HTTP {status_code}. Check provider capability, authentication, and request configuration.")


class ModelOutputError(ModelError):
    """Response was malformed, incomplete, or failed local validation."""


class ModelRefusalError(ModelOutputError):
    """Provider returned a refusal instead of the requested output."""


@dataclass(frozen=True)
class ModelConfig:
    model: str
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    timeout: float = 60.0
    max_retries: int = 2
    retry_delay: float = 0.5
    max_retry_delay: float = 10.0

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if not self.model.strip():
            raise ValueError("A model name is required; use the served name configured on your provider.")
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an HTTP(S) API root without credentials, query, or fragment, e.g. http://localhost:8000/v1")
        if parsed.path.rstrip("/").endswith(("/chat/completions", "/systemone")):
            raise ValueError("Supply the API root as base_url, not the completion/decision endpoint.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be an integer between 0 and 10")
        if any(not math.isfinite(v) or v < 0 for v in (self.retry_delay, self.max_retry_delay)):
            raise ValueError("retry delays must be finite and nonnegative")
        if self.max_retry_delay > 60:
            raise ValueError("max_retry_delay must be at most 60 seconds")

    @classmethod
    def from_env(cls, prefix: str = "LLM") -> "ModelConfig":
        """Read only explicitly named settings; never borrow another provider's key.
        PREFIX_MODEL_NAME is the model; PREFIX_MODEL is accepted as a deprecated alias."""
        model = os.getenv(prefix + "_MODEL_NAME") or os.getenv(prefix + "_MODEL")
        base_url = os.getenv(prefix + "_BASE_URL")
        if not model or not base_url:
            raise ValueError(f"Set {prefix}_MODEL_NAME and {prefix}_BASE_URL")
        return cls(model=model, base_url=base_url, api_key=os.getenv(prefix + "_API_KEY") or None)


class ModelTransport:
    def __init__(self, config: ModelConfig, *, http_client: httpx.Client | None = None):
        self.config = config
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=config.timeout, follow_redirects=False)

    def close(self):
        if self._owns_client:
            self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _wait(self, attempt: int, retry_after: str | None = None):
        delay = self.config.retry_delay * (2 ** attempt)
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    pass
        if not math.isfinite(delay):
            delay = self.config.retry_delay
        time.sleep(max(0, min(delay, self.config.max_retry_delay)))

    def post(self, path: str, body: dict) -> tuple[dict, str | None]:
        url = self.config.base_url.rstrip("/") + "/" + path.lstrip("/")
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._http.post(url, json=body, headers=headers,
                                           timeout=self.config.timeout, follow_redirects=False)
            except httpx.TransportError:
                if attempt < self.config.max_retries:
                    self._wait(attempt)
                    continue
                # Do not include provider bodies, credentials, or prompts in errors.
                raise ModelError("Could not reach the model endpoint within the configured retry/timeout limits.") from None
            request_id = response.headers.get("x-request-id")
            if response.status_code in (408, 429, 500, 502, 503, 504) and attempt < self.config.max_retries:
                self._wait(attempt, response.headers.get("retry-after"))
                continue
            if not 200 <= response.status_code < 300:
                raise ModelHTTPError(response.status_code, request_id)
            try:
                payload = response.json()
            except ValueError:
                raise ModelOutputError("Model endpoint returned a non-JSON response envelope.") from None
            if not isinstance(payload, dict):
                raise ModelOutputError("Model response envelope must be an object.")
            return payload, request_id
        raise AssertionError("Unreachable")
