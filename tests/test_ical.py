from datetime import date

from app.ical import generate_ical


def test_timed_events_use_explicit_utc_for_apple_calendar():
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "report_date": date(2026, 8, 3),
        "before_after": "before",
    }])
    assert "X-WR-TIMEZONE:Asia/Shanghai" in ics
    assert "NAME:FinCal Earnings" in ics
    # 08:00–09:00 America/New_York in August = 12:00–13:00 UTC.
    assert "DTSTART:20260803T120000Z" in ics
    assert "DTEND:20260803T130000Z" in ics
    assert "SUMMARY:AAPL 财报" not in ics
    assert "SUMMARY:AAPL Earnings" in ics
    assert "(US)" not in ics
    assert "TZID=America/New_York" not in ics


def test_all_day_events_remain_timezone_neutral():
    ics = generate_ical([{
        "symbol": "0700.HK", "market": "HK", "company_name": "腾讯控股", "report_date": date(2026, 8, 12),
        "before_after": "",
    }], title_lang="zh")
    assert "DTSTART;VALUE=DATE:20260812" in ics
    assert "DTEND;VALUE=DATE:20260813" in ics
    assert "SUMMARY:0700.HK 腾讯控股 财报" in ics
    assert "(HK)" not in ics
    assert "DTSTAMP:" in ics
    assert "LAST-MODIFIED:" in ics
    assert "CATEGORIES:Earnings" in ics
    assert "TZID=" not in ics


def test_hk_timed_events_use_hong_kong_timezone():
    ics = generate_ical([{
        "symbol": "01810.HK", "market": "HK", "company_name": "小米集团",
        "report_date": date(2026, 8, 19), "before_after": "before",
    }], title_lang="zh")
    # 08:00–09:00 Asia/Hong_Kong = 00:00–01:00 UTC.
    assert "DTSTART:20260819T000000Z" in ics
    assert "DTEND:20260819T010000Z" in ics


def test_text_is_escaped_and_long_utf8_lines_are_folded():
    name = "公司, A\\B; 测试" * 12
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "company_name": name,
        "report_date": date(2026, 8, 3), "before_after": "",
    }], title_lang="zh")
    assert "SUMMARY:AAPL" in ics
    assert "(US)" not in ics
    assert "DESCRIPTION:Company: " in ics
    assert "\\," in ics and "\\;" in ics
    assert "\r\n " in ics
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))
    assert "\\\\" in ics


def test_description_uses_single_escaped_newlines():
    ics = generate_ical([{
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 8, 3), "before_after": "before",
        "fiscal_year": 2026, "fiscal_quarter": 2,
    }])
    assert "\\nFiscal:" in ics
    assert "\\\\nFiscal:" not in ics
