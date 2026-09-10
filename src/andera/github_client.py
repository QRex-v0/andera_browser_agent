from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class GitHubClientError(RuntimeError):
    pass


class GitHubAuthError(GitHubClientError):
    pass


class GitHubHTTPError(GitHubClientError):
    def __init__(self, status: int, url: str, message: str) -> None:
        super().__init__(f"GitHub API request failed with HTTP {status}: {message}")
        self.status = status
        self.url = url
        self.message = message


@dataclass
class GitHubResponse:
    url: str
    status: int
    headers: Dict[str, str]
    data: Any


class GitHubClient:
    """Small authenticated GitHub REST client with local rate pacing.

    The token is read from GITHUB_TOKEN by default and is never stored in
    provenance fields or returned from this class. Callers can use source_urls
    to attach every API URL used for an artifact.
    """

    api_base = "https://api.github.com"
    _host_next_request: Dict[str, float] = {}
    _host_lock = threading.Lock()

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        min_interval_seconds: float = 0.25,
        max_retries: int = 3,
        user_agent: str = "andera-audit-client",
    ) -> None:
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        if not self._token:
            raise GitHubAuthError("GITHUB_TOKEN is required for GitHub REST API calls.")
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.max_retries = max(0, int(max_retries))
        self.user_agent = user_agent
        self.source_urls: List[str] = []

    def get_json(self, path_or_url: str, params: Optional[Dict[str, Any]] = None) -> GitHubResponse:
        url = self._build_url(path_or_url, params=params)
        attempt = 0
        while True:
            self._pace(url)
            try:
                request = Request(url, headers=self._headers())
                with urlopen(request, timeout=30) as response:
                    body = response.read().decode("utf-8")
                    headers = {key: value for key, value in response.headers.items()}
                    status = int(getattr(response, "status", 200))
                    data = json.loads(body) if body else None
                    self.source_urls.append(url)
                    return GitHubResponse(url=url, status=status, headers=headers, data=data)
            except HTTPError as exc:
                self.source_urls.append(url)
                if exc.code in {403, 429} and attempt < self.max_retries:
                    retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
                    if retry_after is not None:
                        time.sleep(retry_after)
                        attempt += 1
                        continue
                raise GitHubHTTPError(exc.code, url, _error_message(exc)) from None
            except URLError as exc:
                raise GitHubClientError(f"GitHub API request failed: {exc.reason}") from exc

    def paginated_json(
        self,
        path_or_url: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        per_page: int = 100,
        max_pages: int = 10,
    ) -> List[Any]:
        query = dict(params or {})
        query.setdefault("per_page", per_page)
        url = self._build_url(path_or_url, params=query)
        items: List[Any] = []
        for _page in range(max(1, int(max_pages))):
            response = self.get_json(url)
            if isinstance(response.data, list):
                items.extend(response.data)
            else:
                items.append(response.data)
            next_url = _next_link(response.headers.get("Link", ""))
            if not next_url:
                break
            url = next_url
        return items

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _build_url(self, path_or_url: str, params: Optional[Dict[str, Any]] = None) -> str:
        raw = str(path_or_url or "").strip()
        if raw.startswith("https://"):
            url = raw
        else:
            url = f"{self.api_base}/{raw.lstrip('/')}"
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params, doseq=True)}"
        return url

    def _pace(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        if not host or self.min_interval_seconds <= 0:
            return
        with self._host_lock:
            now = time.monotonic()
            next_at = self._host_next_request.get(host, 0.0)
            delay = max(0.0, next_at - now)
            self._host_next_request[host] = max(now, next_at) + self.min_interval_seconds
        if delay:
            time.sleep(delay)


def unique_preserve_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _retry_after_seconds(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _error_message(exc: HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
    except Exception:
        pass
    return str(exc.reason or exc)


def _next_link(header: str) -> str:
    for part in (header or "").split(","):
        pieces = part.split(";")
        if len(pieces) < 2:
            continue
        url = pieces[0].strip()
        rel = ";".join(pieces[1:])
        if 'rel="next"' in rel and url.startswith("<") and url.endswith(">"):
            return url[1:-1]
    return ""
