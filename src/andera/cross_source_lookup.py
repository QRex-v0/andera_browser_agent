from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse, urlunparse


SearchFn = Callable[[str], Sequence["SearchCandidate"]]


@dataclass
class PersonIdentity:
    name: str
    github_handle: str = ""
    employer: str = ""
    location: str = ""
    github_url: str = ""
    notes: str = ""


@dataclass
class SearchQuery:
    query: str
    signal: str
    value: str


@dataclass
class SearchCandidate:
    url: str
    title: str = ""
    snippet: str = ""
    source_url: str = ""


@dataclass
class CandidateAssessment:
    candidate: SearchCandidate
    url: str
    accepted: bool
    confidence: str
    score: int
    evidence: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)


@dataclass
class LinkedInLookupResult:
    identity: PersonIdentity
    url: str = ""
    verified: bool = False
    confidence: str = "unverified"
    reason: str = ""
    evidence: List[str] = field(default_factory=list)
    queries: List[SearchQuery] = field(default_factory=list)
    candidates: List[CandidateAssessment] = field(default_factory=list)


@dataclass
class LinkedInLookupSummary:
    total: int
    verified: int
    unverified: int
    reason_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def verified_fraction(self) -> float:
        return self.verified / self.total if self.total else 0.0


@dataclass
class LinkedInLookupBatch:
    rows: List[Dict[str, str]]
    results: List[LinkedInLookupResult]
    summary: LinkedInLookupSummary


_COMMON_WEAK_NAMES = {
    "alex",
    "chris",
    "dan",
    "jason",
    "john",
    "josh",
    "mario",
    "mariano",
    "mike",
    "patrick",
    "peter",
    "rob",
    "sam",
    "shadow",
    "vignesh",
}


def linkedin_queries(identity: PersonIdentity) -> List[SearchQuery]:
    """Build LinkedIn search queries that never use the person's name alone."""
    name = _clean(identity.name)
    signals = _identity_signals(identity)
    if not name or not signals:
        return []
    queries: List[SearchQuery] = []
    for signal, value in signals:
        queries.append(
            SearchQuery(
                query=f'site:linkedin.com/in "{name}" "{value}"',
                signal=signal,
                value=value,
            )
        )
    return _dedupe_queries(queries)


def lookup_linkedin_profile(
    identity: PersonIdentity,
    search: SearchFn,
    *,
    max_candidates: int = 12,
) -> LinkedInLookupResult:
    queries = linkedin_queries(identity)
    if not _name_tokens(identity.name):
        return LinkedInLookupResult(
            identity=identity,
            reason="missing_name",
            queries=queries,
            evidence=["No person name was available to verify."],
        )
    if not queries:
        return LinkedInLookupResult(
            identity=identity,
            reason="missing_disambiguating_signal",
            queries=queries,
            evidence=["No GitHub handle, employer, or location was available; refused name-only search."],
        )
    candidates: List[SearchCandidate] = []
    seen_urls: set[str] = set()
    for query in queries:
        for item in search(query.query):
            canonical = canonical_linkedin_profile_url(item.url)
            key = canonical or _clean_url(item.url)
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            candidates.append(item)
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break
    if not candidates:
        return LinkedInLookupResult(
            identity=identity,
            reason="no_candidates",
            queries=queries,
            evidence=["Search returned no LinkedIn profile candidates."],
        )
    assessments = [assess_linkedin_candidate(identity, item) for item in candidates]
    accepted = [item for item in assessments if item.accepted]
    if not accepted:
        reason = _dominant_rejection_reason(assessments)
        return LinkedInLookupResult(
            identity=identity,
            reason=reason,
            queries=queries,
            candidates=assessments,
            evidence=[_explain_rejection(reason)],
        )
    accepted.sort(key=lambda item: item.score, reverse=True)
    best = accepted[0]
    tied = [item for item in accepted if item.score == best.score and item.url != best.url]
    if tied:
        return LinkedInLookupResult(
            identity=identity,
            reason="ambiguous_candidates",
            queries=queries,
            candidates=assessments,
            evidence=["Multiple LinkedIn profile candidates had equally strong corroborating evidence."],
        )
    return LinkedInLookupResult(
        identity=identity,
        url=best.url,
        verified=True,
        confidence=best.confidence,
        reason="verified",
        evidence=best.evidence,
        queries=queries,
        candidates=assessments,
    )


def assess_linkedin_candidate(
    identity: PersonIdentity,
    candidate: SearchCandidate,
) -> CandidateAssessment:
    url = canonical_linkedin_profile_url(candidate.url)
    evidence_text = _evidence_text(candidate)
    issues: List[str] = []
    evidence: List[str] = []
    score = 0
    if not url:
        return CandidateAssessment(
            candidate=candidate,
            url="",
            accepted=False,
            confidence="unverified",
            score=0,
            issues=["not_linkedin_profile_url"],
        )
    name_ok, name_evidence, name_score = _name_matches(identity.name, evidence_text, url)
    if not name_ok:
        issues.append("name_not_correlated")
    else:
        evidence.extend(name_evidence)
        score += name_score
    signal_evidence, signal_score = _corroborating_signal_evidence(identity, evidence_text)
    if not signal_evidence:
        issues.append("no_disambiguating_evidence")
    else:
        evidence.extend(signal_evidence)
        score += signal_score
    if score < 6:
        issues.append("insufficient_corroboration")
    if _weak_name(identity.name) and signal_score < 4:
        issues.append("weak_name_requires_strong_signal")
    accepted = bool(
        name_ok
        and signal_evidence
        and "insufficient_corroboration" not in issues
        and "weak_name_requires_strong_signal" not in issues
    )
    confidence = _confidence(score) if accepted else "unverified"
    return CandidateAssessment(
        candidate=candidate,
        url=url,
        accepted=accepted,
        confidence=confidence,
        score=score if accepted else 0,
        evidence=evidence,
        issues=issues,
    )


def enrich_linkedin_profile_rows(
    rows: Sequence[Dict[str, str]],
    search: SearchFn,
    *,
    linkedin_column: str = "linkedin url",
) -> LinkedInLookupBatch:
    enriched: List[Dict[str, str]] = []
    results: List[LinkedInLookupResult] = []
    for row in rows:
        item = dict(row)
        identity = identity_from_row(item)
        result = lookup_linkedin_profile(identity, search)
        item[linkedin_column] = result.url if result.verified else ""
        item["_linkedin_confidence"] = result.confidence
        item["_linkedin_reason"] = result.reason
        item["_linkedin_evidence"] = " | ".join(result.evidence)
        if result.verified:
            sources = item.setdefault("_field_source", {})
            if isinstance(sources, dict):
                sources[linkedin_column] = result.url
        enriched.append(item)
        results.append(result)
    summary = summarize_linkedin_results(results)
    return LinkedInLookupBatch(rows=enriched, results=results, summary=summary)


def summarize_linkedin_results(results: Sequence[LinkedInLookupResult]) -> LinkedInLookupSummary:
    reason_counts: Dict[str, int] = {}
    verified = 0
    for result in results:
        if result.verified:
            verified += 1
            continue
        reason_counts[result.reason or "unverified"] = reason_counts.get(result.reason or "unverified", 0) + 1
    return LinkedInLookupSummary(
        total=len(results),
        verified=verified,
        unverified=len(results) - verified,
        reason_counts=reason_counts,
    )


def identity_from_row(row: Dict[str, str]) -> PersonIdentity:
    return PersonIdentity(
        name=_first_field(row, ("name", "full name", "person", "founder", "contributor", "author")),
        github_handle=_first_field(row, ("github handle", "github", "handle", "username", "login")),
        employer=_first_field(row, ("employer", "company", "organization", "org", "workplace")),
        location=_first_field(row, ("location", "city", "country", "region")),
        github_url=_first_field(row, ("github url", "profile url", "url")),
        notes=_first_field(row, ("bio", "notes", "description", "text")),
    )


def canonical_linkedin_profile_url(url: str) -> str:
    raw = _clean_url(url)
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"linkedin.com", "linkedin.cn"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() not in {"in", "pub"}:
        return ""
    if parts[1].lower() in {"dir", "company", "school", "feed", "posts", "pulse"}:
        return ""
    return urlunparse(("https", "www.linkedin.com", "/" + "/".join(parts[:2]), "", "", ""))


def _identity_signals(identity: PersonIdentity) -> List[Tuple[str, str]]:
    signals: List[Tuple[str, str]] = []
    handle = _clean_handle(identity.github_handle)
    if handle:
        signals.append(("github_handle", handle))
        signals.append(("github_url", f"github.com/{handle}"))
    for signal, value in (
        ("employer", identity.employer),
        ("location", identity.location),
        ("github_url", identity.github_url),
    ):
        cleaned = _clean_signal(value)
        if cleaned:
            signals.append((signal, cleaned))
    note_handles = [token for token in re.findall(r"@([A-Za-z0-9][A-Za-z0-9-]{2,38})", identity.notes or "")]
    for token in note_handles[:2]:
        signals.append(("note_handle", token))
    return _dedupe_signals(signals)


def _corroborating_signal_evidence(identity: PersonIdentity, evidence_text: str) -> Tuple[List[str], int]:
    text = _normalize(evidence_text)
    found: List[str] = []
    score = 0
    handle = _clean_handle(identity.github_handle)
    if handle and _contains_token(text, handle):
        found.append(f"GitHub handle {handle!r} appears in candidate evidence.")
        score += 4
    if handle and f"github com {handle}" in text:
        found.append(f"GitHub profile github.com/{handle} appears in candidate evidence.")
        score += 4
    employer = _clean_signal(identity.employer)
    if employer and _phrase_present(text, employer):
        found.append(f"Employer {employer!r} appears in candidate evidence.")
        score += 3
    location = _clean_signal(identity.location)
    if location and _location_present(text, location):
        found.append(f"Location {location!r} appears in candidate evidence.")
        score += 2
    github_url = _clean_signal(identity.github_url)
    if github_url and _phrase_present(text, github_url.replace("https://", "").replace("http://", "")):
        found.append("GitHub URL appears in candidate evidence.")
        score += 4
    return found, score


def _name_matches(name: str, evidence_text: str, url: str) -> Tuple[bool, List[str], int]:
    tokens = _name_tokens(name)
    if not tokens:
        return False, [], 0
    text = _normalize(f"{evidence_text} {url}")
    if len(tokens) == 1:
        token = tokens[0]
        ok = _contains_token(text, token)
        return ok, [f"Name token {token!r} appears in candidate evidence."] if ok else [], 1 if ok else 0
    first, last = tokens[0], tokens[-1]
    ok = _contains_token(text, first) and _contains_token(text, last)
    if ok:
        return True, [f"Name tokens {first!r} and {last!r} appear in candidate evidence."], 3
    if len(tokens) >= 3 and _contains_token(text, tokens[1]) and _contains_token(text, last):
        return True, [f"Name tokens {tokens[1]!r} and {last!r} appear in candidate evidence."], 2
    return False, [], 0


def _confidence(score: int) -> str:
    if score >= 7:
        return "high"
    if score >= 6:
        return "medium"
    return "low"


def _dominant_rejection_reason(assessments: Sequence[CandidateAssessment]) -> str:
    counts: Dict[str, int] = {}
    for item in assessments:
        for issue in item.issues or ["unverified"]:
            counts[issue] = counts.get(issue, 0) + 1
    if not counts:
        return "unverified"
    priority = (
        "no_disambiguating_evidence",
        "weak_name_requires_strong_signal",
        "insufficient_corroboration",
        "name_not_correlated",
        "not_linkedin_profile_url",
    )
    for reason in priority:
        if reason in counts:
            return reason
    return max(counts.items(), key=lambda item: item[1])[0]


def _explain_rejection(reason: str) -> str:
    messages = {
        "no_disambiguating_evidence": "Candidates matched the name but did not corroborate the GitHub handle, employer, or location.",
        "weak_name_requires_strong_signal": "The available name was too weak or common without a strong corroborating signal.",
        "insufficient_corroboration": "Candidates had some corroborating evidence but not enough to safely assert the profile URL.",
        "name_not_correlated": "Candidates did not clearly match the person's name.",
        "not_linkedin_profile_url": "Candidates were not LinkedIn person profile URLs.",
        "unverified": "No candidate had enough evidence to verify the attribution.",
    }
    return messages.get(reason, messages["unverified"])


def _first_field(row: Dict[str, str], names: Iterable[str]) -> str:
    lower = {str(key).lower().strip(): str(value).strip() for key, value in row.items()}
    for name in names:
        if lower.get(name):
            return lower[name]
    for key, value in lower.items():
        if not value:
            continue
        for name in names:
            if name in key:
                return value
    return ""


def _dedupe_queries(queries: Sequence[SearchQuery]) -> List[SearchQuery]:
    seen = set()
    result: List[SearchQuery] = []
    for query in queries:
        key = query.query.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(query)
    return result


def _dedupe_signals(signals: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    seen = set()
    result: List[Tuple[str, str]] = []
    for signal, value in signals:
        cleaned = _clean_signal(value)
        if not cleaned:
            continue
        key = (signal, cleaned.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append((signal, cleaned))
    return result


def _evidence_text(candidate: SearchCandidate) -> str:
    return " ".join(
        part
        for part in (
            candidate.title,
            candidate.snippet,
            candidate.url,
            candidate.source_url,
        )
        if part
    )


def _name_tokens(name: str) -> List[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", _normalize(name))
        if len(token) >= 2 and token not in {"mr", "mrs", "ms", "dr"}
    ]


def _weak_name(name: str) -> bool:
    tokens = _name_tokens(name)
    if len(tokens) <= 1:
        return True
    return tokens[0] in _COMMON_WEAK_NAMES and len(tokens[-1]) <= 3


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clean_handle(handle: str) -> str:
    raw = _clean(handle).lstrip("@")
    if raw.startswith("https://github.com/") or raw.startswith("http://github.com/"):
        raw = raw.rstrip("/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9-]", "", raw)


def _clean_signal(value: str) -> str:
    raw = _clean(value).strip("@")
    raw = re.sub(r"\s*/\s*", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip()


def _clean_url(url: str) -> str:
    return _clean(url)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _contains_token(text: str, token: str) -> bool:
    cleaned = _normalize(token)
    if not cleaned:
        return False
    return bool(re.search(rf"(?:^|\s){re.escape(cleaned)}(?:\s|$)", text))


def _phrase_present(text: str, phrase: str) -> bool:
    normalized = _normalize(phrase)
    if not normalized:
        return False
    parts = [part for part in normalized.split() if len(part) >= 2]
    if not parts:
        return False
    if " ".join(parts) in text:
        return True
    if len(parts) == 1:
        return _contains_token(text, parts[0])
    return all(_contains_token(text, part) for part in parts if part not in {"inc", "llc", "ltd", "co"})


def _location_present(text: str, location: str) -> bool:
    normalized = _normalize(location)
    if not normalized:
        return False
    parts = [part for part in normalized.split() if len(part) >= 3]
    if not parts:
        return False
    if " ".join(parts) in text:
        return True
    return any(_contains_token(text, part) for part in parts)
