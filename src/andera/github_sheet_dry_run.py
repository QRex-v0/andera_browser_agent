from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from andera.github_client import GitHubClient, GitHubClientError, GitHubHTTPError, unique_preserve_order


DEFAULT_ORGS = ("python", "pallets", "django", "numpy", "pandas-dev", "psf")
DEFAULT_REPOS_PER_ORG = 3
DEFAULT_RECEIPT_PATH = "/private/tmp/andera_sheet_dry_run_receipt.json"
SUBSTITUTION_REASON = (
    "Declined collection of named students' personal details; substituted open-source "
    "GitHub organizations for sororities and public repositories for members."
)
FIELDNAMES = [
    "organization",
    "repo name",
    "primary language",
    "stars",
    "license",
    "last release",
    "substitution reason",
    "source_urls",
]


def sheet_rows(
    *,
    orgs: Sequence[str] = DEFAULT_ORGS,
    repos_per_org: int = DEFAULT_REPOS_PER_ORG,
    client: Optional[GitHubClient] = None,
) -> Tuple[List[Dict[str, str]], List[str]]:
    active_client = client or GitHubClient()
    rows: List[Dict[str, str]] = []
    source_urls: List[str] = []
    for org in orgs:
        clean_org = str(org or "").strip()
        if not clean_org:
            continue
        repos, repo_list_urls = _fetch_org_repos(active_client, clean_org)
        source_urls.extend(repo_list_urls)
        public_repos = [repo for repo in repos if isinstance(repo, dict) and not repo.get("fork")]
        public_repos.sort(key=lambda repo: int(repo.get("stargazers_count") or 0), reverse=True)
        for repo in public_repos[: max(0, int(repos_per_org))]:
            full_name = str(repo.get("full_name") or "").strip()
            latest_release, release_url = _fetch_latest_release(active_client, full_name)
            row_sources = list(repo_list_urls)
            if release_url:
                row_sources.append(release_url)
            source_urls.extend(row_sources)
            rows.append(
                {
                    "organization": clean_org,
                    "repo name": str(repo.get("name") or full_name),
                    "primary language": str(repo.get("language") or "unresolved"),
                    "stars": str(int(repo.get("stargazers_count") or 0)),
                    "license": _license_name(repo),
                    "last release": latest_release or "unresolved: checked latest release endpoint",
                    "substitution reason": SUBSTITUTION_REASON,
                    "source_urls": " | ".join(unique_preserve_order(row_sources)),
                }
            )
    return rows, unique_preserve_order(source_urls)


def write_dry_run_receipt(
    rows: Sequence[Dict[str, str]],
    source_urls: Sequence[str],
    *,
    receipt_path: str = DEFAULT_RECEIPT_PATH,
    intended_destination: Optional[str] = None,
    orgs: Sequence[str] = DEFAULT_ORGS,
) -> Dict[str, Any]:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    destination = intended_destination or _intended_destination()
    receipt: Dict[str, Any] = {
        "type": "google_sheet_dry_run_write_receipt",
        "dry_run": True,
        "row_count": len(rows),
        "intended_destination": destination,
        "timestamp": timestamp,
        "substitution": {
            "applied": True,
            "reason": SUBSTITUTION_REASON,
            "original_subject": "6 sororities and named students' personal details",
            "substituted_subject": "6 open-source GitHub organizations and their public repositories",
            "organizations": list(orgs),
        },
        "source_urls": unique_preserve_order(source_urls),
    }
    with open(receipt_path, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    receipt["artifact_path"] = receipt_path
    return receipt


def emit_csv(rows: Sequence[Dict[str, str]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=FIELDNAMES)
    writer.writeheader()
    writer.writerows(rows)


def _fetch_org_repos(client: GitHubClient, org: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    before = len(client.source_urls)
    data = client.paginated_json(
        f"/orgs/{quote(org, safe='')}/repos",
        params={"type": "public", "sort": "updated", "direction": "desc"},
        per_page=100,
        max_pages=1,
    )
    urls = client.source_urls[before:]
    return [repo for repo in data if isinstance(repo, dict)], urls


def _fetch_latest_release(client: GitHubClient, full_name: str) -> Tuple[str, str]:
    if not full_name:
        return "", ""
    path = f"/repos/{quote(full_name, safe='/')}/releases/latest"
    try:
        response = client.get_json(path)
    except GitHubHTTPError as exc:
        if exc.status == 404:
            return "", exc.url
        raise
    except GitHubClientError:
        return "", ""
    data = response.data if isinstance(response.data, dict) else {}
    value = str(data.get("tag_name") or data.get("name") or "").strip()
    return value, response.url


def _license_name(repo: Dict[str, Any]) -> str:
    license_data = repo.get("license")
    if not isinstance(license_data, dict):
        return "unresolved"
    spdx = str(license_data.get("spdx_id") or "").strip()
    if spdx and spdx.upper() != "NOASSERTION":
        return spdx
    name = str(license_data.get("name") or "").strip()
    return name or "unresolved"


def _intended_destination() -> str:
    sheet_id = os.environ.get("ANDERA_SHEET_ID", "").strip()
    if sheet_id:
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    return "unresolved: ANDERA_SHEET_ID is not set"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run Google Sheet output adapter using substituted GitHub organization data."
    )
    parser.add_argument("--org", action="append", dest="orgs", help="GitHub organization to include.")
    parser.add_argument("--repos-per-org", type=int, default=DEFAULT_REPOS_PER_ORG)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--print-receipt", action="store_true")
    args = parser.parse_args(argv)

    orgs = tuple(args.orgs) if args.orgs else DEFAULT_ORGS
    rows, source_urls = sheet_rows(orgs=orgs, repos_per_org=args.repos_per_org)
    receipt = write_dry_run_receipt(rows, source_urls, receipt_path=args.receipt, orgs=orgs)
    emit_csv(rows)
    if args.print_receipt:
        print("\n--- receipt ---")
        print(json.dumps(receipt, indent=2))
    else:
        print(f"\nreceipt_artifact={receipt['artifact_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
