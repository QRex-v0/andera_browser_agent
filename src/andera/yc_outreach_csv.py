from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from andera.constrained_outreach import CollectedFact, generate_founder_outreach_email
from andera.cross_source_lookup import canonical_linkedin_profile_url


YC_COMPANIES_URL = "https://www.ycombinator.com/companies"
AI_TAGS = ("AI", "Artificial Intelligence", "Generative AI", "Machine Learning")


def yc_outreach_rows(*, companies_limit: int = 5) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for hit in _w24_ai_companies()[: max(0, int(companies_limit))]:
        slug = str(hit.get("slug") or "").strip()
        if not slug:
            continue
        company = _company_detail(slug)
        page_url = f"{YC_COMPANIES_URL}/{slug}"
        for founder in company.get("founders") or []:
            founder_name = str(founder.get("full_name") or "").strip()
            linkedin = canonical_linkedin_profile_url(str(founder.get("linkedin_url") or ""))
            email = generate_founder_outreach_email(_facts_for_founder(company, founder)).body
            rows.append(
                {
                    "founder name": founder_name,
                    "LinkedIn URL": linkedin or f"unresolved: checked {page_url}",
                    "personalized cold email": email,
                }
            )
    return rows


def emit_yc_outreach_csv(*, companies_limit: int = 5) -> None:
    rows = yc_outreach_rows(companies_limit=companies_limit)
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=["founder name", "LinkedIn URL", "personalized cold email"],
    )
    writer.writeheader()
    writer.writerows(rows)


def _w24_ai_companies() -> List[Dict[str, Any]]:
    opts = _algolia_opts()
    url = f"https://{opts['app']}-dsn.algolia.net/1/indexes/YCCompany_production/query"
    filters = [["batch:Winter 2024"], [f"tags:{tag}" for tag in AI_TAGS]]
    params = {
        "hitsPerPage": 50,
        "page": 0,
        "facetFilters": json.dumps(filters),
    }
    payload = json.dumps({"params": urlencode(params)}).encode("utf-8")
    response = _fetch_json(
        url,
        data=payload,
        headers={
            "X-Algolia-Application-Id": opts["app"],
            "X-Algolia-API-Key": opts["key"],
            "Content-Type": "application/json",
        },
    )
    hits = response.get("hits") if isinstance(response, dict) else []
    return hits if isinstance(hits, list) else []


def _company_detail(slug: str) -> Dict[str, Any]:
    page = _fetch_text(f"{YC_COMPANIES_URL}/{slug}")
    match = re.search(r'data-page="(.*?)"', page, flags=re.S)
    if not match:
        return {}
    payload = json.loads(html.unescape(match.group(1)))
    props = payload.get("props") if isinstance(payload, dict) else {}
    company = props.get("company") if isinstance(props, dict) else {}
    return company if isinstance(company, dict) else {}


def _facts_for_founder(company: Dict[str, Any], founder: Dict[str, Any]) -> List[CollectedFact]:
    source = f"{YC_COMPANIES_URL}/{company.get('slug')}"
    return [
        CollectedFact("yc-company-name", "company_name", str(company.get("name") or ""), source_url=source),
        CollectedFact("yc-founder-name", "founder_name", str(founder.get("full_name") or ""), source_url=source),
        CollectedFact("yc-founder-role", "founder_role", str(founder.get("title") or "Founder"), source_url=source),
        CollectedFact("yc-batch", "yc_batch", str(company.get("batch_name") or company.get("batch") or ""), source_url=source),
    ]


def _algolia_opts() -> Dict[str, str]:
    page = _fetch_text(YC_COMPANIES_URL)
    match = re.search(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});", page, flags=re.S)
    if not match:
        raise RuntimeError("Could not find YC Algolia search configuration.")
    payload = json.loads(match.group(1))
    return {"app": str(payload["app"]), "key": str(payload["key"])}


def _fetch_json(url: str, *, data: bytes, headers: Dict[str, str]) -> Any:
    request = Request(
        url,
        data=data,
        headers={**headers, "User-Agent": "andera-yc-outreach"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "ignore")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Emit YC W24 AI founder outreach CSV.")
    parser.add_argument("--companies", type=int, default=5)
    args = parser.parse_args(argv)
    emit_yc_outreach_csv(companies_limit=args.companies)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
