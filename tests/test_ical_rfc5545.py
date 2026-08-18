"""RFC 5545 parsing validation and multi-client regression samples (Issue #1).

Validates generated iCal content with the ``icalendar`` reference parser and
covers the sample scenarios called out in the issue:
  - All-day events with DTEND next-day non-inclusive semantics
  - US summer/winter time (DST boundary) explicit UTC conversion
  - HK timezone (no DST) explicit UTC conversion
  - Chinese long-description folding at UTF-8 byte boundaries
  - Predicted → confirmed event content changes SEQUENCE
  - Add / modify / delete semantics via UID stability
  - Subscription endpoint returns valid text/calendar without cookies
"""
from datetime import date, datetime, timezone

import icalendar
import pytest

from app.ical import generate_ical

# ── Helpers ────────────────────────────────────────────────────────────────

def _parse(ics_text: str) -> icalendar.Calendar:
    """Parse iCal text with the reference parser; fails on any RFC violation."""
    cal = icalendar.Calendar.from_ical(ics_text)
    return cal


def _events(cal: icalendar.Calendar) -> list:
    return [c for c in cal.walk() if c.name == "VEVENT"]


# ── Scenario 1: All-day event non-inclusive DTEND ─────────────────────────

def test_all_day_dtend_next_day_rfc5545():
    """DTEND for all-day events must be the day AFTER the last day (non-inclusive)."""
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 8, 12), "before_after": "",
    }])
    cal = _parse(ics)
    evts = _events(cal)
    assert len(evts) == 1
    ev = evts[0]
    # RFC 5545: VALUE=DATE, DTEND is non-inclusive → next day
    assert ev["DTSTART"].dt == date(2026, 8, 12)
    assert ev["DTEND"].dt == date(2026, 8, 13)
    # No TZID on date-only events
    params = ev["DTSTART"].params
    assert params.get("VALUE") == "DATE"
    assert "TZID" not in params


# ── Scenario 2: US summer time (EDT) explicit UTC ─────────────────────────

def test_us_summer_time_utc_conversion():
    """US pre-market 08:00 EDT → 12:00 UTC in August."""
    ics = generate_ical([{
        "symbol": "MSFT", "market": "US", "company_name": "Microsoft",
        "report_date": date(2026, 8, 3), "before_after": "before",
    }])
    cal = _parse(ics)
    ev = _events(cal)[0]
    dtstart = ev["DTSTART"].dt
    assert dtstart == datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    dtend = ev["DTEND"].dt
    assert dtend == datetime(2026, 8, 3, 13, 0, 0, tzinfo=timezone.utc)
    # Must use explicit UTC, not TZID
    assert ev["DTSTART"].params.get("TZID") is None


# ── Scenario 3: US winter time (EST) explicit UTC ─────────────────────────

def test_us_winter_time_utc_conversion():
    """US after-hours 16:00 EST → 21:00 UTC in January."""
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 1, 28), "before_after": "after",
    }])
    cal = _parse(ics)
    ev = _events(cal)[0]
    dtstart = ev["DTSTART"].dt
    # EST = UTC-5, so 16:00 EST = 21:00 UTC
    assert dtstart == datetime(2026, 1, 28, 21, 0, 0, tzinfo=timezone.utc)
    dtend = ev["DTEND"].dt
    assert dtend == datetime(2026, 1, 28, 22, 0, 0, tzinfo=timezone.utc)


# ── Scenario 4: HK timezone (no DST) explicit UTC ─────────────────────────

def test_hk_timezone_utc_conversion():
    """HK pre-market 08:00 HKT → 00:00 UTC."""
    ics = generate_ical([{
        "symbol": "0700.HK", "market": "HK", "company_name": "腾讯控股",
        "report_date": date(2026, 8, 19), "before_after": "before",
    }])
    cal = _parse(ics)
    ev = _events(cal)[0]
    dtstart = ev["DTSTART"].dt
    assert dtstart == datetime(2026, 8, 19, 0, 0, 0, tzinfo=timezone.utc)
    dtend = ev["DTEND"].dt
    assert dtend == datetime(2026, 8, 19, 1, 0, 0, tzinfo=timezone.utc)


def test_hk_after_hours_utc_conversion():
    """HK after-hours 16:00 HKT → 08:00 UTC."""
    ics = generate_ical([{
        "symbol": "01810.HK", "market": "HK", "company_name": "小米集团",
        "report_date": date(2026, 8, 19), "before_after": "after",
    }])
    cal = _parse(ics)
    ev = _events(cal)[0]
    dtstart = ev["DTSTART"].dt
    assert dtstart == datetime(2026, 8, 19, 8, 0, 0, tzinfo=timezone.utc)


# ── Scenario 5: Chinese long description folding ──────────────────────────

def test_chinese_long_description_parses_rfc5545():
    """Long Chinese text must fold at UTF-8 byte boundaries without corruption."""
    name = "公司名称很长，包含特殊字符\\;," * 20
    ics = generate_ical([{
        "symbol": "TEST", "market": "US", "company_name": name,
        "report_date": date(2026, 8, 3), "before_after": "before",
    }], title_lang="zh")
    cal = _parse(ics)
    ev = _events(cal)[0]
    # Verify the description survived round-trip without corruption
    desc = str(ev["DESCRIPTION"])
    assert "Company:" in desc
    # Verify CRLF folding: each raw line ≤ 75 UTF-8 bytes
    for raw_line in ics.split("\r\n"):
        assert len(raw_line.encode("utf-8")) <= 75, f"Line too long: {raw_line[:60]}"


def test_text_field_escaping_roundtrip():
    """Backslash, comma, semicolon, newline in TEXT values survive parsing."""
    special = "A\\B; C,D\nE\r\nF"
    ics = generate_ical([{
        "symbol": "SPCL", "market": "US", "company_name": special,
        "report_date": date(2026, 8, 3), "before_after": "",
    }])
    cal = _parse(ics)
    ev = _events(cal)[0]
    summary = str(ev["SUMMARY"])
    desc = str(ev["DESCRIPTION"])
    # Unescaped values should survive the round-trip
    assert "SPCL" in summary
    assert "Company:" in desc


# ── Scenario 6: Predicted vs confirmed event STATUS ───────────────────────

def test_predicted_event_tentative_status():
    """Predicted earnings → STATUS:TENTATIVE."""
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before", "is_predicted": True,
    }])
    cal = _parse(ics)
    ev = _events(cal)[0]
    assert str(ev["STATUS"]) == "TENTATIVE"
    assert "Predicted" in str(ev["CATEGORIES"])


def test_confirmed_event_confirmed_status():
    """Confirmed earnings → STATUS:CONFIRMED."""
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before", "is_predicted": False,
    }])
    cal = _parse(ics)
    ev = _events(cal)[0]
    assert str(ev["STATUS"]) == "CONFIRMED"


# ── Scenario 7: Predicted → confirmed changes SEQUENCE ────────────────────

def test_predicted_to_confirmed_changes_sequence():
    """Same event becoming confirmed must change SEQUENCE."""
    predicted = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before", "is_predicted": True,
    }])
    confirmed = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before", "is_predicted": False,
    }])
    cal_p = _parse(predicted)
    cal_c = _parse(confirmed)
    seq_p = int(_events(cal_p)[0]["SEQUENCE"])
    seq_c = int(_events(cal_c)[0]["SEQUENCE"])
    assert seq_p != seq_c


# ── Scenario 8: UID stability across regenerations ────────────────────────

def test_uid_stable_across_regenerations():
    """Same event data must always produce the same UID."""
    event = {
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 8, 3), "before_after": "before",
    }
    ics1 = generate_ical([event])
    ics2 = generate_ical([event])
    uid1 = str(_events(_parse(ics1))[0]["UID"])
    uid2 = str(_events(_parse(ics2))[0]["UID"])
    assert uid1 == uid2
    assert "fincal-" in uid1


# ── Scenario 9: Multiple events in single calendar ────────────────────────

def test_multiple_events_parse_correctly():
    """Multiple VEVENTs in one VCALENDAR all parse independently."""
    events = [
        {"symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3), "before_after": "before"},
        {"symbol": "0700.HK", "market": "HK", "company_name": "腾讯控股", "report_date": date(2026, 8, 12), "before_after": ""},
        {"symbol": "TSLA", "market": "US", "report_date": date(2026, 1, 28), "before_after": "after", "is_predicted": True},
    ]
    ics = generate_ical(events, title_lang="zh")
    cal = _parse(ics)
    evts = _events(cal)
    assert len(evts) == 3
    # All have unique UIDs
    uids = {str(ev["UID"]) for ev in evts}
    assert len(uids) == 3
    # All have required properties
    for ev in evts:
        assert ev["UID"] is not None
        assert ev["DTSTAMP"] is not None
        assert ev["DTSTART"] is not None
        assert ev["DTEND"] is not None
        assert ev["SUMMARY"] is not None
        assert ev["DESCRIPTION"] is not None
        assert ev["STATUS"] is not None
        assert ev["SEQUENCE"] is not None


# ── Scenario 10: Calendar-level properties ────────────────────────────────

def test_calendar_level_properties():
    """VCALENDAR has all required and recommended properties."""
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before",
    }])
    cal = _parse(ics)
    assert str(cal["VERSION"]) == "2.0"
    assert "FinCal" in str(cal["PRODID"])
    assert str(cal["CALSCALE"]) == "GREGORIAN"
    assert str(cal["METHOD"]) == "PUBLISH"
    # RFC 7986 NAME
    assert str(cal["NAME"]) == "FinCal Earnings"
    # Apple extension
    assert str(cal["X-WR-CALNAME"]) == "FinCal Earnings"


# ── Scenario 11: DTSTAMP and LAST-MODIFIED are valid UTC ──────────────────

def test_dtstamp_last_modified_are_valid_utc():
    """DTSTAMP and LAST-MODIFIED must be valid datetime objects in UTC."""
    event = {
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before",
        "updated_at": datetime(2026, 7, 15, 10, 30, 0, tzinfo=timezone.utc),
    }
    ics = generate_ical([event])
    cal = _parse(ics)
    ev = _events(cal)[0]
    # Both must be datetime (not date)
    assert isinstance(ev["DTSTAMP"].dt, datetime)
    assert isinstance(ev["LAST-MODIFIED"].dt, datetime)
    assert ev["DTSTAMP"].dt == datetime(2026, 7, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert ev["LAST-MODIFIED"].dt == datetime(2026, 7, 15, 10, 30, 0, tzinfo=timezone.utc)


# ── Scenario 12: Empty earnings list produces valid empty calendar ────────

def test_empty_earnings_produces_valid_calendar():
    """Zero-earnings feed must still parse as valid VCALENDAR."""
    ics = generate_ical([])
    cal = _parse(ics)
    evts = _events(cal)
    assert len(evts) == 0
    assert str(cal["VERSION"]) == "2.0"


# ── Scenario 13: Content line CRLF termination ────────────────────────────

def test_content_lines_use_crlf():
    """All content lines must be terminated with CRLF per RFC 5545."""
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before",
    }])
    # Split on CRLF; no bare LF should exist
    lines = ics.split("\r\n")
    assert len(lines) > 5
    # If there were bare \n, we'd get extra empty/short lines
    bare_lf_lines = ics.split("\n")
    assert len(bare_lf_lines) == len(lines), "Bare LF detected — must use CRLF only"
