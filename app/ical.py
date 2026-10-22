"""iCal (.ics) feed generation for user watchlist."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, fiscal


_MARKET_TZ = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}


def _utc_ical_timestamp(report_date: date, hhmmss: str, market: str = "US") -> str:
    """Convert a market-local wall-clock time to an explicit UTC iCal value."""
    local = datetime.combine(
        report_date,
        time(int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])),
        tzinfo=_MARKET_TZ.get(market, _MARKET_TZ["US"]),
    )
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# Market-local report times are converted to UTC; Mac Calendar then renders
# them in the user's configured timezone.


def _escape_ical(value: object) -> str:
    return (str(value).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n")
            .replace("\r", "\\n").replace("\n", "\\n"))


def _fold_ical_lines(lines: list[str]) -> list[str]:
    """Fold content lines at UTF-8 byte boundaries (RFC 5545)."""
    folded: list[str] = []
    for line in lines:
        raw = line.encode("utf-8")
        if len(raw) <= 75:
            folded.append(line)
            continue
        first = True
        while raw:
            limit = 75 if first else 74
            chunk = raw[:limit]
            while True:
                try:
                    text = chunk.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    chunk = chunk[:-1]
            folded.append(("" if first else " ") + text)
            raw = raw[len(chunk):]
            first = False
    return folded


#: Every word the DESCRIPTION body and the calendar name use, one entry per
#: key and both languages side by side (Issue #97).
#:
#: Before this table the body was hard-coded English while the summary and its
#: markers already followed ``title_lang`` (Issue #55), so a ``zh`` subscription
#: — the default for ``/ical/{token}`` — read "中文标题 + English body".  The
#: ``en`` column holds the exact literals the feed emitted before the table
#: existed, so an ``en`` feed stays byte-identical; only ``zh`` gains new text.
#:
#: ``comparability`` is a format template rather than a bare label: the whole
#: line's punctuation is a language decision too, so ``{wording}`` and
#: ``{reason}`` are filled in by :func:`_comparability_note`.
_LABELS = {
    "company": {"en": "Company", "zh": "公司"},
    "fiscal": {"en": "Fiscal", "zh": "财季"},
    "timing": {"en": "Timing", "zh": "时间"},
    "date_source": {"en": "Date source", "zh": "日期来源"},
    "eps_estimate": {"en": "EPS Est", "zh": "EPS 预期"},
    "eps_actual": {"en": "EPS Actual", "zh": "EPS 实际"},
    "predicted_warning": {"en": "⚠ Predicted date (not confirmed)",
                          "zh": "⚠ 预测日期（未经确认）"},
    "currency_unknown": {"en": "currency unknown", "zh": "币种未知"},
    "pre_market": {"en": "Pre-market", "zh": "盘前"},
    "after_hours": {"en": "After-hours", "zh": "盘后"},
    "comparability": {"en": "Comparability: unavailable ({wording}; {reason})",
                      "zh": "对比：不可用（{wording}；{reason}）"},
    "calendar_name": {"en": "FinCal Earnings", "zh": "FinCal 财报日历"},
}

#: Label/value divider per language: the English feed's ``": "`` is what every
#: subscriber already receives, the Chinese one uses the full-width colon.
_SEPARATOR = {"en": ": ", "zh": "："}


def _label(key: str, title_lang: str = "en") -> str:
    """One label from :data:`_LABELS`; unknown languages read as ``en``."""
    entry = _LABELS.get(key) or {}
    return entry.get(title_lang) or entry.get("en") or ""


def _labelled(key: str, value: str, title_lang: str = "en") -> str:
    """``Company: Apple`` / ``公司：苹果`` — label, language divider, value."""
    return f"{_label(key, title_lang)}{_SEPARATOR.get(title_lang, _SEPARATOR['en'])}{value}"


def _status_marker(date_status: str, title_lang: str) -> str:
    """The ``[预测]`` / ``[未确认]`` suffix, in the feed's language (Issue #55).

    These two markers used to be hard-coded Chinese, so a ``lang=en`` subscription
    mixed ``Earnings`` with ``[预测]``. One table for both languages keeps the
    vocabulary in a single place — the same reason ``_comparability_note`` has
    one.
    """
    labels = {
        "predicted": {"zh": " [预测]", "en": " [Predicted]"},
        "unavailable": {"zh": " [未确认]", "en": " [Unconfirmed]"},
    }
    return labels.get(date_status, {}).get(title_lang) or labels.get(date_status, {}).get("zh") or ""


def _company_label(event: dict, title_lang: str) -> str:
    """The company name an event shows, in the feed's language (Issue #55).

    ``company_name`` is the display name (Kurumi's, usually Chinese) and
    ``company_name_en`` the Latin one. An English feed uses the Latin name and
    falls back to the display name when the sync never resolved one — the
    documented rule: no empty titles, no double spaces, and never a fake
    placeholder. The gap itself is counted by the stock_names stage.
    """
    company = str(event.get("company_name") or "").strip()
    if title_lang == "zh":
        return company
    name_en = str(event.get("company_name_en") or "").strip()
    return name_en or company


def _comparability_note(reason: str, title_lang: str = "en") -> str:
    """One description line explaining why no surprise is shown (Issue #61).

    The event carries both figures, so a subscriber may reasonably expect a
    comparison; the line states the reason in words (the language the summary
    already follows) and keeps the machine-readable code for support.  The
    surrounding wording — ``Comparability: unavailable`` and the brackets —
    comes from :data:`_LABELS` since Issue #97, so the sentence is not half
    English, half Chinese.
    """
    labels = {
        "currency_mismatch": {"en": "different currencies", "zh": "币种不同"},
        "currency_unknown": {"en": "currency undeclared", "zh": "币种未标明"},
        "basis_mismatch": {"en": "different accounting bases", "zh": "口径不同"},
        "basis_unverified": {"en": "accounting base unverified", "zh": "口径未经同一来源确认"},
    }
    wording = labels.get(reason, {}).get(title_lang) or labels.get(reason, {}).get("en") or reason
    return _label("comparability", title_lang).format(wording=wording, reason=reason)


def _attributed_amount(label: str, value, currency: str | None,
                       title_lang: str = "en") -> str:
    """Render ``EPS Est: 3.34 (USD)`` — a figure always carries its currency.

    Issue #61: this description used to print the estimate and the actual side by
    side with no unit at all, so a TWD actual next to a USD estimate read like an
    enormous beat in every subscribed calendar. When the provider never declared
    the currency the line says so explicitly instead of falling back to the
    listing's currency — that placeholder follows ``title_lang`` (Issue #97);
    the label itself is chosen and passed by the caller.
    """
    if value in (None, ""):
        return ""
    placeholder = _label("currency_unknown", title_lang)
    separator = _SEPARATOR.get(title_lang, _SEPARATOR["en"])
    return f"{label}{separator}{value} ({currency or placeholder})"


def _event_moment(event: dict) -> datetime | None:
    """Parse the row's write timestamp (``updated_at``).

    Returns ``None`` when the event carries no usable timestamp, so callers can
    pick their own deterministic fallback.  Naive timestamps are read as UTC.
    ``created_at`` used to be read as a fallback, but ``earnings`` has no such
    column (Issue #73), so it could only ever be ``None``.
    """
    value = event.get("updated_at")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _stable_event_stamp(event: dict, report_date: date) -> str:
    """Return a stable UTC modification stamp for an event.

    Prefer the database update timestamp. The report date is a deterministic
    fallback; request time must never be used for LAST-MODIFIED.
    """
    moment = _event_moment(event) or datetime.combine(report_date, time.min, tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


#: Base instant for emitted ``SEQUENCE`` values.  Subtracting a fixed epoch keeps
#: the number small — RFC 5545 §3.3.8 defines ``integer`` as a signed 32-bit
#: value — while staying strictly monotone in the row's ``updated_at``.  With
#: ``_SEQUENCE_STRIDE`` = 2 the 32-bit ceiling falls around 2054.
_SEQUENCE_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)

#: One revision step per second, leaving the low bit to the revision rank.
_SEQUENCE_STRIDE = 2

#: Revision rank: a tentative (predicted) date is always revised *into* a
#: confirmed one, never the other way round, so the confirmed row must outrank
#: the prediction even when both were written inside the same second.
_SEQUENCE_RANK_PREDICTED = 0
_SEQUENCE_RANK_CONFIRMED = 1


def _is_tentative(event: dict) -> bool:
    """True while the row is still an unconfirmed prediction for this period.

    Mirrors the "still a prediction" signals the generator already keys off
    (``is_predicted`` and the derived ``date_status``).  It only feeds the
    revision rank; the emitted ``STATUS`` / summary markers are unchanged.
    """
    return bool(event.get("is_predicted")) or event.get("date_status") == "predicted"


def _event_sequence(event: dict, report_date: date) -> int:
    """Return the VEVENT revision number (RFC 5545 §3.8.7.4).

    ``SEQUENCE`` must *increase* whenever a component is revised: clients
    (Outlook in particular) discard an update whose sequence is not newer than
    the copy they already hold.  The previous implementation hashed the event
    content, which has nothing to do with revision order — a confirmed date
    could be emitted with a *smaller* sequence than the prediction it replaced,
    and consecutive estimate corrections went up and down at random, so
    subscribers silently kept a stale date (Issue #37).

    Revision order now comes from the row's write timestamp, which every
    earnings write path sets (``updated_at = NOW()`` in the sync, prediction,
    seed and reconcile upserts), making the value monotone non-decreasing by
    construction:

    * seconds since :data:`_SEQUENCE_EPOCH` order the revisions themselves;
    * the low-order rank makes tentative → confirmed strictly greater even when
      both rows were written within the same second;
    * unchanged content (same timestamp) yields the same number, so a feed that
      did not change never makes clients treat it as a new revision.

    Rows without a timestamp (in-memory callers, legacy rows) fall back to their
    report date so the value stays deterministic between requests.
    """
    moment = _event_moment(event) or datetime.combine(report_date, time.min, tzinfo=timezone.utc)
    seconds = int((moment - _SEQUENCE_EPOCH).total_seconds())
    rank = _SEQUENCE_RANK_PREDICTED if _is_tentative(event) else _SEQUENCE_RANK_CONFIRMED
    return max(0, seconds * _SEQUENCE_STRIDE + rank)


def _event_uid(event: dict, report_date: date) -> str:
    """Persistent VEVENT identity for an earnings record (RFC 5545).

    RFC 5545 treats the UID as the identity of a calendar component: when the
    same logical event is revised — a predicted date confirmed, or a date
    rescheduled — it must keep the same UID so clients update the event in place
    rather than delete + recreate it.  Deriving the UID from ``report_date``
    (the previous behaviour) broke that for exactly this revision path.  The
    persistent identity is therefore the fiscal key
    (``symbol + market + fiscal_year + fiscal_quarter``); when those are missing
    we fall back to the report date (Issue #40).
    """
    symbol = event.get("symbol", "?")
    market = event.get("market", "US")
    fy = event.get("fiscal_year")
    fq = event.get("fiscal_quarter")
    if fy not in (None, "") and fq not in (None, ""):
        return f"fincal-{symbol}-{market}-FY{fy}-Q{fq}@{config.APP_NAME}"
    return f"fincal-{symbol}-{market}-{report_date.strftime('%Y%m%d')}@{config.APP_NAME}"


def _authority_key(event: dict, report_date: date) -> tuple:
    """Rank the row that should represent a fiscal period.

    One fiscal period can appear as several ``earnings`` rows — a predicted row
    next to a confirmed one, or two confirmed rows when the provider rescheduled
    the event (Issue #50).  The ranking lives in :func:`app.fiscal.authority_key`
    so iCal, the API, the export and the reconciliation script all pick the same
    row; this wrapper only keeps the ``report_date`` argument the generator
    already has at hand.
    """
    return fiscal.authority_key(event, report_date)


def _dedupe_events(earnings: list[dict]) -> tuple[list[str], dict]:
    """Collapse rows that map to the same UID so one fiscal period emits one VEVENT.

    Returns ``(uid_order, chosen)`` where ``uid_order`` preserves first-seen
    order and ``chosen`` maps each UID to its authoritative ``(event, date)``.
    Rows without a usable report date are dropped (mirrors the generator).
    """
    uid_order: list[str] = []
    chosen: dict[str, tuple[dict, date]] = {}
    for e in earnings:
        report_date = e.get("report_date")
        if isinstance(report_date, datetime):
            report_date = report_date.date()
        if not report_date:
            continue
        uid = _event_uid(e, report_date)
        if uid not in chosen:
            chosen[uid] = (e, report_date)
            uid_order.append(uid)
        elif _authority_key(e, report_date) < _authority_key(*chosen[uid]):
            chosen[uid] = (e, report_date)
    return uid_order, chosen


def generate_ical(earnings: list[dict], user_email: str = "", title_lang: str = "en") -> str:
    """Generate iCal content from earnings records."""
    # The calendar's own name follows the feed's language too (Issue #97):
    # otherwise the calendar list shows "FinCal Earnings" next to Chinese event
    # titles, and two subscriptions become hard to tell apart.
    calendar_name = _label("calendar_name", title_lang)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FinCal//Earnings Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"NAME:{_escape_ical(calendar_name)}",
        f"X-WR-CALNAME:{_escape_ical(calendar_name)}",
        "X-WR-TIMEZONE:Asia/Shanghai",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]

    uid_order, chosen = _dedupe_events(earnings)
    for uid in uid_order:
        e, report_date = chosen[uid]

        symbol = e.get("symbol", "?")
        market = e.get("market", "US")
        company = _company_label(e, title_lang)
        report_type = e.get("report_type", "Q")
        fq = e.get("fiscal_quarter", "")
        fy = e.get("fiscal_year", "")
        before_after = e.get("before_after", "")

        # Determine event time
        if before_after == "before":
            hour_start, hour_end = "080000", "090000"
            time_label = _label("pre_market", title_lang)
        elif before_after == "after":
            hour_start, hour_end = "160000", "170000"
            time_label = _label("after_hours", title_lang)
        else:
            # All-day event
            hour_start, hour_end = None, None
            time_label = ""

        fq_str = f"Q{fq}" if fq else ""
        is_pred = e.get("is_predicted", False)
        # Issue #55: the status marker follows the feed's language; a reported
        # event carries none. `_status_marker` owns the wording for both.
        date_status = e.get("date_status", "")
        date_source = e.get("date_source", "")
        source_tag = "" if date_status == "reported" else _status_marker(date_status, title_lang)
        if not source_tag and date_status != "reported" and is_pred:
            source_tag = _status_marker("predicted", title_lang)
        # The symbol already carries the market suffix for HK (e.g. 0700.HK).
        # Bare symbols are product-default US symbols, so do not append a
        # redundant `(US)` / `(HK)` marker. Put the company name next to code;
        # `company` is already resolved for the feed's language (Issue #55).
        summary_parts = [str(symbol)]
        if company:
            summary_parts.append(str(company))
        if fq_str:
            summary_parts.append(fq_str)
        summary_parts.append("财报" if title_lang == "zh" else "Earnings")
        summary = " ".join(summary_parts) + source_tag
        # Company names use the cached canonical name; title_lang decides which of
        # the two stored spellings is shown (Issue #55).
        comparison_reason = e.get("comparison_unavailable_reason") or fiscal.comparison_unavailable_reason(e)
        # Issue #97: every label here comes from `_LABELS`, so the body follows
        # the same `title_lang` as the summary instead of staying English.
        desc_parts = [_labelled("company", company, title_lang) if company else "",
                      _labelled("fiscal", f"FY{fy} {fq_str}", title_lang) if fy else "",
                      _labelled("timing", time_label, title_lang) if time_label else "",
                      _label("predicted_warning", title_lang) if is_pred else "",
                      _labelled("date_source", date_source, title_lang)
                      if date_source and date_source != "unknown" else "",
                      _attributed_amount(_label("eps_estimate", title_lang), e.get("eps_estimate"),
                                         fiscal.declared_attribution(e, "estimate_currency"),
                                         title_lang),
                      _attributed_amount(_label("eps_actual", title_lang), e.get("eps_actual"),
                                         fiscal.declared_attribution(e, "actual_currency"),
                                         title_lang),
                      _comparability_note(comparison_reason, title_lang) if comparison_reason else ""]
        desc = "\n".join(p for p in desc_parts if p)

        dt_str = report_date.strftime("%Y%m%d")
        # UID is the persistent fiscal identity computed during dedup, so a
        # predicted -> confirmed (or rescheduled) revision keeps the same UID
        # and clients update the event in place (Issue #40).
        stamp = _stable_event_stamp(e, report_date)
        sequence = _event_sequence(e, report_date)

        lines.append("BEGIN:VEVENT")
        lines.append(f"SEQUENCE:{sequence}")
        lines.append(f"UID:{_escape_ical(uid)}")
        lines.append(f"DTSTAMP:{stamp}")
        lines.append(f"LAST-MODIFIED:{stamp}")
        lines.append(f"SUMMARY:{_escape_ical(summary)}")
        lines.append(f"DESCRIPTION:{_escape_ical(desc)}")
        lines.append(f"CATEGORIES:Earnings{',Predicted' if is_pred else ''}")

        if hour_start:
            # Explicit UTC timestamps are interpreted consistently by Apple Calendar.
            lines.append(f"DTSTART:{_utc_ical_timestamp(report_date, hour_start, market)}")
            lines.append(f"DTEND:{_utc_ical_timestamp(report_date, hour_end or hour_start, market)}")
        else:
            # Date-only events are intentionally timezone-neutral in iCalendar.
            lines.append(f"DTSTART;VALUE=DATE:{dt_str}")
            lines.append(f"DTEND;VALUE=DATE:{(report_date + timedelta(days=1)).strftime('%Y%m%d')}")

        lines.append(f"STATUS:{'TENTATIVE' if is_pred else 'CONFIRMED'}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_ical_lines(lines))
