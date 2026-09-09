from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class CollectedFact:
    """A deterministic fact captured from evidence, not model knowledge."""

    fact_id: str
    field: str
    value: str
    source_url: str = ""
    source_locator: Dict[str, Any] = field(default_factory=dict)
    captured_at: str = ""
    evidence_refs: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class EmailClaim:
    text: str
    fact_ids: List[str]


@dataclass
class GeneratedEmail:
    subject: str
    body: str
    personalized: bool
    reason: str
    used_facts: List[CollectedFact] = field(default_factory=list)
    claims: List[EmailClaim] = field(default_factory=list)
    omitted_reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class OutreachOptions:
    sender_name: str = "Andera"
    sender_company: str = "Andera"
    call_to_action: str = "Would a brief conversation next week be useful?"
    value_proposition: str = "I would like to share a brief idea if it is relevant."
    max_description_chars: int = 180


_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "founder_name": ("founder_name", "founder", "name", "full_name", "person"),
    "founder_role": ("founder_role", "role", "title", "position"),
    "company_name": ("company_name", "company", "startup", "organization", "org"),
    "company_description": (
        "company_description",
        "description",
        "what_it_does",
        "company_blurb",
        "about",
    ),
    "yc_batch": ("yc_batch", "batch", "y_combinator_batch", "yc"),
}


def generate_founder_outreach_email(
    facts: Sequence[CollectedFact],
    options: Optional[OutreachOptions] = None,
) -> GeneratedEmail:
    opts = options or OutreachOptions()
    inventory = _FactInventory(facts)
    selected = _select_outreach_facts(inventory)
    missing = _missing_personalization_reasons(selected)
    if missing:
        return _generic_email(opts, missing)

    used: List[CollectedFact] = []
    claims: List[EmailClaim] = []

    founder = selected["founder_name"]
    company = selected["company_name"]
    description = selected.get("company_description")
    role = selected.get("founder_role")
    batch = selected.get("yc_batch")

    used.extend([founder, company])
    subject = f"Quick note for {company.value}"
    first_name = _first_name(founder.value)
    lines = [f"Hi {first_name}," if first_name else "Hi,"]

    if description:
        description_text = _trim_sentence(description.value, opts.max_description_chars)
        text = f"I saw that {company.value} describes itself as {description_text}."
        lines.append("")
        lines.append(text)
        used.extend([description])
        claims.append(EmailClaim(text=text, fact_ids=[company.fact_id, description.fact_id]))

    role_batch_text, role_batch_fact_ids, role_batch_used = _role_batch_sentence(
        founder=founder,
        company=company,
        role=role,
        batch=batch,
    )
    if role_batch_text:
        lines.append(role_batch_text)
        used.extend(role_batch_used)
        claims.append(EmailClaim(text=role_batch_text, fact_ids=role_batch_fact_ids))

    lines.extend(
        [
            "",
            opts.value_proposition,
            opts.call_to_action,
            "",
            f"Best,",
            opts.sender_name,
        ]
    )
    used = _dedupe_facts(used)
    return GeneratedEmail(
        subject=subject,
        body="\n".join(lines),
        personalized=True,
        reason="personalized_from_collected_facts",
        used_facts=used,
        claims=claims,
    )


def generate_founder_outreach_batch(
    rows: Sequence[Dict[str, Any]],
    *,
    provenance_fields: Optional[Sequence[Dict[str, Any]]] = None,
    options: Optional[OutreachOptions] = None,
) -> List[GeneratedEmail]:
    return [
        generate_founder_outreach_email(
            facts_from_row(row, row_index=index, provenance_fields=provenance_fields),
            options=options,
        )
        for index, row in enumerate(rows)
    ]


def facts_from_row(
    row: Dict[str, Any],
    *,
    row_index: int = 0,
    provenance_fields: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[CollectedFact]:
    provenance = _provenance_by_column(provenance_fields or [], row_index)
    facts: List[CollectedFact] = []
    for column, raw in row.items():
        if str(column).startswith("_"):
            continue
        value = str(raw or "").strip()
        if not value:
            continue
        source = provenance.get(str(column), {})
        facts.append(
            CollectedFact(
                fact_id=f"row:{row_index}:{_slug(str(column))}",
                field=str(column),
                value=value,
                source_url=str(source.get("source_url") or row.get("_source_url") or ""),
                source_locator=dict(source.get("source_locator") or {"row": row_index, "column": str(column)}),
                captured_at=str(source.get("captured_at") or ""),
                evidence_refs=[str(item) for item in source.get("evidence_refs") or []],
            )
        )
    return facts


def outreach_verification_summary(emails: Sequence[GeneratedEmail]) -> Dict[str, Any]:
    total = len(emails)
    personalized = sum(1 for item in emails if item.personalized)
    reasons: Dict[str, int] = {}
    for item in emails:
        if item.personalized:
            continue
        key = ", ".join(item.omitted_reasons) or item.reason or "unpersonalized"
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "total": total,
        "personalized": personalized,
        "unpersonalized": total - personalized,
        "personalized_fraction": personalized / total if total else 0.0,
        "unpersonalized_reasons": reasons,
    }


class _FactInventory:
    def __init__(self, facts: Sequence[CollectedFact]) -> None:
        self._facts = [fact for fact in facts if _clean(fact.value)]

    def first(self, canonical: str) -> Optional[CollectedFact]:
        aliases = _FIELD_ALIASES.get(canonical, (canonical,))
        alias_keys = {_field_key(item) for item in aliases}
        for fact in self._facts:
            key = _field_key(fact.field)
            if key in alias_keys:
                return fact
        for fact in self._facts:
            key = _field_key(fact.field)
            if any(alias in key or key in alias for alias in alias_keys):
                return fact
        return None


def _select_outreach_facts(inventory: _FactInventory) -> Dict[str, CollectedFact]:
    selected: Dict[str, CollectedFact] = {}
    for name in _FIELD_ALIASES:
        fact = inventory.first(name)
        if fact:
            selected[name] = fact
    return selected


def _missing_personalization_reasons(selected: Dict[str, CollectedFact]) -> List[str]:
    missing: List[str] = []
    if not selected.get("founder_name"):
        missing.append("missing_founder_name")
    if not selected.get("company_name"):
        missing.append("missing_company_name")
    if not (
        selected.get("company_description")
        or selected.get("founder_role")
        or selected.get("yc_batch")
    ):
        missing.append("missing_personal_detail")
    return missing


def _generic_email(opts: OutreachOptions, reasons: Sequence[str]) -> GeneratedEmail:
    body = "\n".join(
        [
            "Hi,",
            "",
            opts.value_proposition,
            opts.call_to_action,
            "",
            "Best,",
            opts.sender_name,
        ]
    )
    return GeneratedEmail(
        subject="Quick note",
        body=body,
        personalized=False,
        reason="insufficient_collected_facts",
        omitted_reasons=list(reasons),
    )


def _role_batch_sentence(
    *,
    founder: CollectedFact,
    company: CollectedFact,
    role: Optional[CollectedFact],
    batch: Optional[CollectedFact],
) -> Tuple[str, List[str], List[CollectedFact]]:
    if role and batch:
        text = (
            f"I also saw {founder.value} listed as {role.value} at {company.value}, "
            f"with {company.value} in {batch.value}."
        )
        return text, [founder.fact_id, role.fact_id, company.fact_id, batch.fact_id], [role, batch]
    if role:
        text = f"I also saw {founder.value} listed as {role.value} at {company.value}."
        return text, [founder.fact_id, role.fact_id, company.fact_id], [role]
    if batch:
        text = f"I also saw {company.value} listed in {batch.value}."
        return text, [company.fact_id, batch.fact_id], [batch]
    return "", [], []


def _provenance_by_column(
    fields: Sequence[Dict[str, Any]],
    row_index: int,
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in fields:
        locator = item.get("source_locator") or {}
        if locator.get("row") != row_index:
            continue
        column = locator.get("column")
        if column is not None:
            result[str(column)] = dict(item)
            continue
        path = str(item.get("path") or "")
        prefix = f"csv.rows[{row_index}]."
        if path.startswith(prefix):
            result[path[len(prefix) :]] = dict(item)
    return result


def _dedupe_facts(facts: Iterable[CollectedFact]) -> List[CollectedFact]:
    seen = set()
    result: List[CollectedFact] = []
    for fact in facts:
        if fact.fact_id in seen:
            continue
        seen.add(fact.fact_id)
        result.append(fact)
    return result


def _first_name(name: str) -> str:
    match = re.search(r"[A-Za-z][A-Za-z'-]*", name or "")
    return match.group(0) if match else ""


def _trim_sentence(text: str, limit: int) -> str:
    cleaned = _clean(text)
    if len(cleaned) <= limit:
        return cleaned
    trimmed = cleaned[: max(0, limit - 1)].rstrip()
    trimmed = re.sub(r"\s+\S*$", "", trimmed).rstrip(" ,;:")
    return f"{trimmed}..."


def _field_key(value: str) -> str:
    return _slug(value).replace("-", "_")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()
