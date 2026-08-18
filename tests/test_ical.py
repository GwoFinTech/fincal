from datetime import date

from app.ical import generate_ical


def test_timed_events_use_explicit_utc_for_apple_calendar():
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before",
    }])
    assert "X-WR-TIMEZONE:Asia/Shanghai" in ics
    # 08:00–09:00 America/New_York in August = 12:00–13:00 UTC.
    assert "DTSTART:20260803T120000Z" in ics
    assert "DTEND:20260803T130000Z" in ics
    assert "TZID=America/New_York" not in ics


def test_all_day_events_remain_timezone_neutral():
    ics = generate_ical([{
        "symbol": "0700.HK", "market": "HK", "report_date": date(2026, 8, 12),
        "before_after": "",
    }])
    assert "DTSTART;VALUE=DATE:20260812" in ics
    assert "DTEND;VALUE=DATE:20260812" in ics
    assert "TZID=" not in ics
