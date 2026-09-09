from __future__ import annotations

from andera.constrained_outreach import (
    CollectedFact,
    generate_founder_outreach_email,
)


def _pocket_facts() -> list[CollectedFact]:
    source = "https://www.ycombinator.com/companies/pocket"
    return [
        CollectedFact(
            "yc-pocket-company",
            "company_name",
            "Pocket",
            source_url=source,
            source_locator={"lines": "21"},
        ),
        CollectedFact(
            "yc-pocket-description",
            "company_description",
            (
                "Pocket is a Y Combinator W26 company building a premium hardware plus software "
                "product in the AI voice recorder space. We are obsessed with quality and speed. "
                "We are scaling very fast in the US and now European markets and building a device "
                "experience that can become the category leader."
            ),
            source_url=source,
            source_locator={"lines": "40-42"},
        ),
        CollectedFact(
            "yc-pocket-founder",
            "founder_name",
            "Gabriel Dymowski",
            source_url=source,
            source_locator={"lines": "64-66"},
        ),
        CollectedFact(
            "yc-pocket-founder-role",
            "founder_role",
            "Founder",
            source_url=source,
            source_locator={"lines": "70"},
        ),
        CollectedFact(
            "yc-pocket-batch",
            "yc_batch",
            "Winter 2026",
            source_url=source,
            source_locator={"lines": "23, 195-198"},
        ),
    ]


def test_personalized_outreach_is_fluent_and_claim_cited() -> None:
    email = generate_founder_outreach_email(_pocket_facts())

    assert email.personalized is True
    assert email.subject == "15 minutes about Pocket?"
    assert "Would you be open to a 15-minute call next week?" in email.body
    assert "describes itself as Pocket is" not in email.body
    assert "scaling...." not in email.body
    assert "Pocket caught my eye because it is building a premium hardware plus software product" in email.body
    assert "I also saw you listed as Founder, with Pocket in Winter 2026." in email.body
    assert "the AI voice recorder device experience" in email.body

    used_ids = {fact.fact_id for fact in email.used_facts}
    claim_ids = {fact_id for claim in email.claims for fact_id in claim.fact_ids}
    assert claim_ids <= used_ids
    assert {
        "yc-pocket-company",
        "yc-pocket-description",
        "yc-pocket-founder",
        "yc-pocket-founder-role",
        "yc-pocket-batch",
    } <= used_ids


def test_thin_outreach_stays_generic_and_unpersonalized() -> None:
    email = generate_founder_outreach_email(
        [
            CollectedFact("founder", "founder_name", "Grace Hopper"),
            CollectedFact("company", "company_name", "Compiler Labs"),
        ]
    )

    assert email.personalized is False
    assert email.used_facts == []
    assert email.claims == []
    assert "Grace" not in email.body
    assert "Compiler Labs" not in email.body
    assert "Would you be open to a 15-minute call next week?" in email.body
