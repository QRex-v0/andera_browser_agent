"""Isolated GitHub REST helper that implements fetch_pr_detail.

The production fetcher may live in andera.pr_detail (Codex). Callers should
import fetch_pr_detail from there when present; this module is the fallback
with the same signature. The token is read from the environment only and is
never returned.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from andera.env import github_token, load_local_env

_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_MIN_INTERVAL = 0.25
_last_request = 0.0


def fetch_pr_detail(owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
    load_local_env()
    number = int(pr_number)
    base = f"/repos/{owner}/{repo}/pulls/{number}"
    source_urls: List[str] = []
    pr = _get_json(base, source_urls)
    commits = _paginated_json(f"{base}/commits", source_urls)
    reviews = _paginated_json(f"{base}/reviews", source_urls)
    merged_at = str(pr.get("merged_at") or "").strip() if isinstance(pr, dict) else ""
    merged_by = _login_or_name(pr.get("merged_by")) if isinstance(pr, dict) else ""
    return {
        "pr_number": number,
        "committers": _committers(commits if isinstance(commits, list) else []),
        "reviewers": _reviewers(pr if isinstance(pr, dict) else {}, reviews if isinstance(reviews, list) else []),
        "merged_by": merged_by or None,
        "merged_at": merged_at or None,
        "source_urls": _unique(source_urls),
    }


def _get_json(path: str, source_urls: List[str], params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{_API}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"
    headers = {
        "Accept": _ACCEPT,
        "User-Agent": "andera-pr-detail",
        "X-GitHub-Api-Version": _API_VERSION,
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    last_error = None
    for attempt in range(4):
        _pace()
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                source_urls.append(url)
                return json.loads(body) if body else None
        except HTTPError as exc:
            source_urls.append(url)
            last_error = RuntimeError(f"GitHub API request failed with HTTP {exc.code}")
            if exc.code in {403, 429} and attempt < 3:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = max(1.0, float(retry_after or 1.5 * (attempt + 1)))
                except (TypeError, ValueError):
                    delay = 1.5 * (attempt + 1)
                time.sleep(delay)
                continue
            raise last_error from None
        except URLError as exc:
            raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc
    if last_error:
        raise last_error
    raise RuntimeError("GitHub API request failed")


def _pace() -> None:
    global _last_request
    now = time.monotonic()
    delay = _MIN_INTERVAL - (now - _last_request)
    if delay > 0:
        time.sleep(delay)
    _last_request = time.monotonic()


def _paginated_json(path: str, source_urls: List[str], *, per_page: int = 100) -> List[Any]:
    payload = _get_json(path, source_urls, params={"per_page": per_page})
    if isinstance(payload, list):
        return payload
    return [payload] if payload is not None else []


def _committers(commits: List[Dict[str, Any]]) -> List[str]:
    values: List[str] = []
    for commit in commits:
        committer = commit.get("committer")
        if isinstance(committer, dict) and committer.get("login"):
            values.append(str(committer["login"]))
            continue
        commit_data = commit.get("commit")
        if isinstance(commit_data, dict):
            nested = commit_data.get("committer")
            if isinstance(nested, dict) and nested.get("name"):
                values.append(str(nested["name"]))
    return _unique(values)


def _reviewers(pr: Dict[str, Any], reviews: List[Dict[str, Any]]) -> List[str]:
    approved: List[str] = []
    for review in reviews:
        if str(review.get("state") or "").upper() != "APPROVED":
            continue
        reviewer = _login_or_name(review.get("user"))
        if reviewer:
            approved.append(f"approved: {reviewer}")
    requested: List[str] = []
    for reviewer in pr.get("requested_reviewers") or []:
        name = _login_or_name(reviewer)
        if name:
            requested.append(f"requested: {name}")
    for team in pr.get("requested_teams") or []:
        slug = str(team.get("slug") or team.get("name") or "").strip()
        if slug:
            requested.append(f"requested team: {slug}")
    return _unique(approved + requested)


def _login_or_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("name") or "").strip()
    return ""


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result
