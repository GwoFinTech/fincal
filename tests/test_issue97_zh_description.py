"""Issue #97: a ``zh`` subscription's body and calendar name must be Chinese.

Issue #55 localized the SUMMARY and its ``[预测]`` marker, but the DESCRIPTION
body and the calendar-level ``NAME`` / ``X-WR-CALNAME`` stayed hard-coded
English.  ``/ical/{token}`` defaults to ``lang=zh`` and the subscribe dialog
sends ``lang: 'zh'``, so the *default* subscriber read a Chinese title above an
English body — "Company: 阿斯麦 / Fiscal: FY2026 Q3 / Timing: Pre-market" next to
``⚠ Predicted date (not confirmed)`` — while the app itself says 「盘前」「EPS 预期」.

The fix is one text table (``app.ical._LABELS``) that holds every label in both
languages, so a key can never be translated for one language only; the ``en``
column holds the exact literals the feed emitted before, which is why an ``en``
feed stays byte-identical (pinned by a golden test below).

Covered here:

* the zh body's labels are Chinese and none of the old English labels survive;
* the timing line and the predicted-date warning follow the same language;
* the comparability sentence is localized as a whole, reason code included;
* an undeclared currency is named in Chinese instead of ``currency unknown``;
* the calendar name is Chinese for ``zh`` and unchanged for ``en``;
* every table key carries both languages (a half-translated table fails here);
* an unknown language falls back to English instead of mixing languages;
* the folded zh feed still parses with ``icalendar`` at ≤75 bytes per line.
"""
import re
import sys
from datetime import date
from pathlib import Path

import icalendar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ical import _LABELS, generate_ical  # noqa: E402

#: Labels the body used to hard-code; none of them may appear in a zh feed.
ENGLISH_LABELS = (
    "Company:",
    "Fiscal:",
    "Timing:",
    "Date source:",
    "EPS Est",
    "EPS Actual",
    "currency unknown",
    "Predicted date (not confirmed)",
    "Comparability:",
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


# ── helpers ────────────────────────────────────────────────────────────────

def _row(**overrides) -> dict:
    """A fully populated reported-or-predicted row; overrides win."""
    row = {
        "symbol": "ASML", "market": "US",
        "company_name": "阿斯麦", "company_name_en": "ASML",
        "report_date": date(2026, 10, 15), "before_after": "before",
        "fiscal_year": 2026, "fiscal_quarter": 3,
        "date_status": "reported", "is_predicted": False,
        "date_source": "longbridge",
        "eps_estimate": 3.34, "eps_actual": 4.0,
        "estimate_currency": "USD", "actual_currency": "EUR",
    }
    row.update(overrides)
    return row


def _unfold(ics: str) -> str:
    return ics.replace("\r\n ", "")


def _property(ics: str, name: str) -> list[str]:
    prefix = f"{name}:"
    return [line[len(prefix):] for line in _unfold(ics).split("\r\n")
            if line.startswith(prefix)]


def _body(ics: str) -> str:
    """The first event's DESCRIPTION, unescaped into its real lines."""
    raw = _property(ics, "DESCRIPTION")[0]
    return raw.replace("\\n", "\n").replace("\\;", ";").replace("\\,", ",")


def _events(ics: str) -> list:
    return [c for c in icalendar.Calendar.from_ical(ics).walk() if c.name == "VEVENT"]


# ── the zh body ────────────────────────────────────────────────────────────

def test_zh_body_uses_chinese_labels_only():
    """Acceptance 1: no English label survives in a zh DESCRIPTION."""
    body = _body(generate_ical([_row()], title_lang="zh"))

    for label in ("公司：", "财季：", "时间：", "日期来源：", "EPS 预期：", "EPS 实际："):
        assert label in body, f"missing Chinese label {label!r} in:\n{body}"
    for english in ENGLISH_LABELS:
        assert english not in body, f"English label {english!r} leaked into zh body:\n{body}"


def test_zh_body_localizes_timing_and_the_predicted_warning():
    """盘前/盘后 and the prediction notice follow the feed's language."""
    pre = _body(generate_ical([_row(is_predicted=True, date_status="predicted")], title_lang="zh"))
    assert "时间：盘前" in pre
    assert "⚠ 预测日期（未经确认）" in pre
    assert "Pre-market" not in pre

    after = _body(generate_ical([_row(before_after="after")], title_lang="zh"))
    assert "时间：盘后" in after


def test_zh_body_localizes_the_whole_comparability_sentence():
    """The reason code stays machine-readable; everything around it is Chinese."""
    body = _body(generate_ical([_row()], title_lang="zh"))
    assert "对比：不可用（币种不同；currency_mismatch）" in body


def test_zh_body_names_an_undeclared_currency_in_chinese():
    body = _body(generate_ical(
        [_row(estimate_currency=None, actual_currency=None)], title_lang="zh"))
    assert "(币种未知)" in body
    assert "currency unknown" not in body


def test_zh_calendar_name_is_chinese():
    """Acceptance 3: the calendar list entry follows the feed's language too."""
    zh = generate_ical([], title_lang="zh")
    name = _property(zh, "NAME")[0]
    assert CJK_RE.search(name), name
    assert _property(zh, "X-WR-CALNAME")[0] == name

    en = generate_ical([], title_lang="en")
    assert _property(en, "NAME")[0] == "FinCal Earnings"
    assert _property(en, "X-WR-CALNAME")[0] == "FinCal Earnings"


# ── the en body must not move ──────────────────────────────────────────────

def test_en_body_is_byte_identical_to_the_pre_change_wording():
    """Acceptance 2: the en body is pinned to the exact bytes it always had."""
    body = _body(generate_ical([_row()], title_lang="en"))
    assert body == (
        "Company: ASML\n"
        "Fiscal: FY2026 Q3\n"
        "Timing: Pre-market\n"
        "Date source: longbridge\n"
        "EPS Est: 3.34 (USD)\n"
        "EPS Actual: 4.0 (EUR)\n"
        "Comparability: unavailable (different currencies; currency_mismatch)"
    )


def test_en_body_keeps_the_predicted_warning_literal():
    body = _body(generate_ical([_row(is_predicted=True, date_status="predicted")], title_lang="en"))
    assert "⚠ Predicted date (not confirmed)" in body
    assert "Timing: Pre-market" in body


# ── the table itself ───────────────────────────────────────────────────────

def test_every_label_carries_both_languages():
    """Acceptance 4: a key added to one language only fails here."""
    for key, entry in _LABELS.items():
        assert set(entry) == {"en", "zh"}, f"{key} is not bilingual: {sorted(entry)}"
        for lang, value in entry.items():
            assert value.strip(), f"{key}/{lang} is empty"


def test_unknown_language_falls_back_to_english():
    """No half-language feed: an unsupported lang reads as en, never a blank."""
    ics = generate_ical([_row()], title_lang="ja")
    body = _body(ics)
    assert "Company: ASML" in body
    assert "公司：" not in body
    assert _property(ics, "NAME")[0] == "FinCal Earnings"


# ── RFC 5545 still holds for the longer Chinese lines ──────────────────────

def test_zh_feed_folds_and_parses_with_icalendar():
    """Acceptance 5: long Chinese lines fold at UTF-8 boundaries and parse back."""
    name = "阿斯麦控股公司，" * 12
    ics = generate_ical([_row(company_name=name, before_after="after")], title_lang="zh")

    for raw_line in ics.split("\r\n"):
        assert len(raw_line.encode("utf-8")) <= 75, raw_line[:40]

    events = _events(ics)
    assert len(events) == 1
    parsed = str(events[0]["DESCRIPTION"])
    assert f"公司：{name}" in parsed
    assert "时间：盘后" in parsed
    assert "EPS 实际：4.0 (EUR)" in parsed
