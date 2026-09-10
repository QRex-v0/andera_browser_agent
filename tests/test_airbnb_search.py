from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from andera import airbnb_search as ab

CARDS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "airbnb" / "result_cards.json").read_text())


def test_next_week_window_starts_on_the_following_monday():
    # 2026-09-09 is a Wednesday.
    assert ab.next_week_window(date(2026, 9, 9)) == (date(2026, 9, 14), date(2026, 9, 21))


def test_next_week_window_from_a_sunday_skips_the_whole_next_week_start():
    # 2026-09-13 is a Sunday, so its week's Monday is 2026-09-07.
    assert ab.next_week_window(date(2026, 9, 13)) == (date(2026, 9, 14), date(2026, 9, 21))


def test_next_week_window_honours_a_shorter_stay():
    checkin, checkout = ab.next_week_window(date(2026, 9, 9), nights=3)
    assert (checkout - checkin).days == 3


def test_next_week_window_rejects_a_zero_night_stay():
    with pytest.raises(ValueError):
        ab.next_week_window(date(2026, 9, 9), nights=0)


def test_discover_date_params_reads_the_names_off_the_page():
    html = (
        '{"listingParamOverrides":{"adults":1,"checkin":"2026-10-19","checkout":"2026-10-24"}}'
        '{"listingParamOverrides":{"adults":1,"checkin":"2026-09-13","checkout":"2026-09-18"}}'
    )
    assert ab.discover_date_params(html) == ("checkin", "checkout")


def test_discover_date_params_prefers_the_most_common_pair():
    html = (
        '{"start_date":"2026-01-01","end_date":"2026-01-05"}'
        '{"arrive":"2026-02-01","depart":"2026-02-08"}'
        '{"arrive":"2026-03-01","depart":"2026-03-08"}'
    )
    assert ab.discover_date_params(html) == ("arrive", "depart")


def test_discover_date_params_ignores_backwards_ranges():
    html = '{"published":"2026-05-05","expired":"2026-01-01"}'
    with pytest.raises(ab.AirbnbSearchError):
        ab.discover_date_params(html)


def test_with_dates_replaces_rather_than_appends():
    url = "https://www.airbnb.com/s/Lake-Tahoe/homes?query=Lake%20Tahoe&checkin=2020-01-01"
    dated = ab.with_dates(url, date(2026, 9, 14), date(2026, 9, 21), ("checkin", "checkout"))
    query = parse_qs(urlsplit(dated).query)
    assert query["checkin"] == ["2026-09-14"]
    assert query["checkout"] == ["2026-09-21"]
    assert query["query"] == ["Lake Tahoe"]


def test_parse_card_reads_a_home_listing():
    listing = ab.parse_card(CARDS[0]["text"], CARDS[0]["href"])
    assert listing.title == "Rustic modern with a wood-burning fireplace"
    assert listing.property_line == "Condo in Kings Beach"
    assert listing.location == "Kings Beach"
    assert listing.size == ["3 bedrooms", "4 beds", "2 baths"]
    assert listing.price_total == "$1,286"
    assert listing.price_before_discount == "$1,376"
    assert listing.stay_length == "for 7 nights"
    assert listing.rating == 5.0
    assert listing.reviews == 6
    assert listing.badges == ["Guest favorite"]
    assert listing.url == "https://www.airbnb.com/rooms/1650212003268506724"


def test_parse_card_unswaps_a_hotel_whose_name_comes_first():
    listing = ab.parse_card(CARDS[1]["text"], CARDS[1]["href"])
    assert listing.title == "The Iceberg Tahoe"
    assert listing.property_line == "Hotel in South Lake Tahoe"
    assert listing.price_total == "$1,837"
    assert listing.stay_length == "for 7 nights"


def test_parse_card_records_a_shortened_availability_window():
    listing = ab.parse_card(CARDS[2]["text"], CARDS[2]["href"])
    assert listing.stay_length == "for 6 nights"
    assert "available Sep 14 to 20" in listing.notes
    assert listing.badges == []


def test_parse_card_handles_a_listing_with_no_bedroom_count():
    listing = ab.parse_card(CARDS[3]["text"], CARDS[3]["href"])
    assert listing.property_line == "Condo in Tahoe Vista"
    assert listing.title == "Fireplace, hot tub, and pool near Northstar"
    assert listing.size == ["1 sofa bed", "1 bath"]


def test_parse_card_marks_a_listing_with_no_reviews():
    listing = ab.parse_card(CARDS[4]["text"], CARDS[4]["href"])
    assert listing.rating is None
    assert "no reviews yet" in listing.summary()


def test_summary_names_price_stay_and_rating():
    summary = ab.parse_card(CARDS[0]["text"], CARDS[0]["href"]).summary()
    assert "Condo in Kings Beach" in summary
    assert "$1,286 (down from $1,376) for 7 nights" in summary
    assert "rated 5.0 from 6 reviews" in summary


def test_render_report_states_the_window_and_a_short_count():
    listings = [ab.parse_card(card["text"], card["href"]) for card in CARDS]
    result = ab.SearchResult(
        place="Lake Tahoe",
        checkin="2026-09-14",
        checkout="2026-09-21",
        nights=7,
        date_params=("checkin", "checkout"),
        location_only_url="https://www.airbnb.com/s/Lake-Tahoe/homes",
        dated_url="https://www.airbnb.com/s/Lake-Tahoe/homes?checkin=2026-09-14&checkout=2026-09-21",
        requested=30,
        listings=listings,
    )
    report = ab.render_report(result)
    assert "2026-09-14 to 2026-09-21" in report
    assert "checkin, checkout" in report
    assert f"Looked at {len(listings)} of the 30 requested places" in report
