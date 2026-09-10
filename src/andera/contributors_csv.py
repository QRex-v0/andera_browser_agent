from __future__ import annotations

import argparse
import csv
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from andera.cross_source_lookup import canonical_linkedin_profile_url
from andera.github_client import GitHubClient, GitHubClientError


_LINKEDIN_RE = re.compile(r"https?://(?:[A-Za-z0-9.-]+\.)?linkedin\.com/[^\s\"'<>]+", re.I)


def contributor_rows(owner: str, repo: str, *, limit: int = 30) -> List[Dict[str, str]]:
    client = GitHubClient()
    contributors = client.paginated_json(
        f"/repos/{owner}/{repo}/contributors",
        params={"per_page": max(1, int(limit))},
        max_pages=1,
    )[:limit]
    rows: List[Dict[str, str]] = []
    for contributor in contributors:
        login = str(contributor.get("login") or "").strip()
        profile_url = str(contributor.get("html_url") or f"https://github.com/{login}")
        profile = _fetch_profile(client, contributor, login)
        social_accounts = _fetch_social_accounts(client, login)
        linkedin = _self_declared_linkedin(profile, social_accounts)
        rows.append(
            {
                "github": login,
                "name": str(profile.get("name") or login),
                "linkedin URL": linkedin or f"unresolved: checked {profile_url}",
            }
        )
    return rows


def emit_contributors_csv(owner: str, repo: str, *, limit: int = 30) -> None:
    rows = contributor_rows(owner, repo, limit=limit)
    writer = csv.DictWriter(sys.stdout, fieldnames=["github", "name", "linkedin URL"])
    writer.writeheader()
    writer.writerows(rows)


def _fetch_profile(client: GitHubClient, contributor: Dict[str, Any], login: str) -> Dict[str, Any]:
    url = str(contributor.get("url") or "").strip()
    try:
        if url:
            data = client.get_json(url).data
        else:
            data = client.get_json(f"/users/{quote(login, safe='')}").data
        return data if isinstance(data, dict) else {}
    except GitHubClientError:
        return {"login": login, "name": "", "html_url": contributor.get("html_url") or f"https://github.com/{login}"}


def _fetch_social_accounts(client: GitHubClient, login: str) -> List[Dict[str, Any]]:
    if not login:
        return []
    try:
        data = client.get_json(f"/users/{quote(login, safe='')}/social_accounts").data
    except GitHubClientError:
        return []
    return data if isinstance(data, list) else []


def _self_declared_linkedin(profile: Dict[str, Any], social_accounts: List[Dict[str, Any]]) -> str:
    fields = [str(profile.get("blog") or ""), str(profile.get("bio") or "")]
    for account in social_accounts:
        provider = str(account.get("provider") or "").lower()
        url = str(account.get("url") or "")
        if provider == "linkedin" or "linkedin.com/" in url.lower():
            fields.append(url)
    for field in fields:
        direct = canonical_linkedin_profile_url(field)
        if direct:
            return direct
        for match in _LINKEDIN_RE.findall(field):
            direct = canonical_linkedin_profile_url(match.rstrip(").,;"))
            if direct:
                return direct
    return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Emit contributor LinkedIn CSV from GitHub profile data.")
    parser.add_argument("owner")
    parser.add_argument("repo")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    emit_contributors_csv(args.owner, args.repo, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
