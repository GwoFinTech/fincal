"""iCal (.ics) feed generation for user watchlist."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config


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


def _stable_event_stamp(event: dict, report_date: date) -> str:
    """Return a stable UTC modification stamp for an event.

    Prefer the database update timestamp. The report date is a deterministic
    fallback; request time must never be used for LAST-MODIFIED.
    """
    value = event.get("updated_at") or event.get("created_at")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except ValueError:
            pass
    return datetime.combine(report_date, time.min, tzinfo=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _event_sequence(event: dict, summary: str, desc: str, report_date: date) -> int:
    """Derive a stable positive SEQUENCE from the event content."""
    payload = "|".join((str(event.get(key, "")) for key in (
        "symbol", "market", "report_date", "report_type", "fiscal_year",
        "fiscal_quarter", "before_after", "is_predicted", "company_name",
        "eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual",
    ))) + f"|{summary}|{desc}|{report_date.isoformat()}"
    return int.from_bytes(__import__("hashlib").sha256(payload.encode()).digest()[:4], "big") & 0x7FFFFFFF


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
    """Rank the row that should represent a UID group.

    One fiscal period can appear as both a predicted and a confirmed row in the
    same batch (defensive — ``mark_confirmed`` normally removes the predicted
    row upstream).  The authoritative row is the confirmed (non-predicted) one;
    ties break on the most recently updated, then the latest report date.
    """
    predicted = 1 if event.get("is_predicted") else 0
    ts = event.get("updated_at") or event.get("created_at")
    ts_key = -ts.timestamp() if isinstance(ts, datetime) else 0
    return (predicted, ts_key, -report_date.toordinal())


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
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FinCal//Earnings Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "NAME:FinCal Earnings",
        "X-WR-CALNAME:FinCal Earnings",
        "X-WR-TIMEZONE:Asia/Shanghai",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]

    uid_order, chosen = _dedupe_events(earnings)
    for uid in uid_order:
        e, report_date = chosen[uid]

        symbol = e.get("symbol", "?")
        market = e.get("market", "US")
        company = e.get("company_name", "")
        report_type = e.get("report_type", "Q")
        fq = e.get("fiscal_quarter", "")
        fy = e.get("fiscal_year", "")
        before_after = e.get("before_after", "")

        # Determine event time
        if before_after == "before":
            hour_start, hour_end = "080000", "090000"
            time_label = "Pre-market"
        elif before_after == "after":
            hour_start, hour_end = "160000", "170000"
            time_label = "After-hours"
        else:
            # All-day event
            hour_start, hour_end = None, None
            time_label = ""

        fq_str = f"Q{fq}" if fq else ""
        is_pred = e.get("is_predicted", False)
        pred_marker = " [预测]" if is_pred else ""
        # Issue #17: explicit source status in summary
        date_status = e.get("date_status", "")
        date_source = e.get("date_source", "")
        if date_status == "predicted":
            source_tag = " [预测]"
        elif date_status == "reported":
            source_tag = ""
        elif date_status == "unavailable":
            source_tag = " [未确认]"
        else:
            source_tag = pred_marker
        # The symbol already carries the market suffix for HK (e.g. 0700.HK).
        # Bare symbols are product-default US symbols, so do not append a
        # redundant `(US)` / `(HK)` marker. Put the company name next to code.
        summary_parts = [str(symbol)]
        if company:
            summary_parts.append(str(company) if title_lang == "zh" else str(e.get("company_name_en") or company))
        if fq_str:
            summary_parts.append(fq_str)
        summary_parts.append("财报" if title_lang == "zh" else "Earnings")
        summary = " ".join(summary_parts) + source_tag
        # Company names use the cached canonical name; title_lang controls the
        # event wording because the current earnings schema has one name field.
        # Keep the human-readable summary free of duplicate market labels.
        desc_parts = [f"Company: {company}" if company else "",
                      f"Fiscal: FY{fy} {fq_str}" if fy else "",
                      f"Timing: {time_label}" if time_label else "",
                      f"⚠ Predicted date (not confirmed)" if is_pred else "",
                      f"Date source: {date_source}" if date_source and date_source != "unknown" else "",
                      f"EPS Est: {e.get('eps_estimate')}" if e.get('eps_estimate') else "",
                      f"EPS Actual: {e.get('eps_actual')}" if e.get('eps_actual') else ""]
        desc = "\n".join(p for p in desc_parts if p)

        dt_str = report_date.strftime("%Y%m%d")
        # UID is the persistent fiscal identity computed during dedup, so a
        # predicted -> confirmed (or rescheduled) revision keeps the same UID
        # and clients update the event in place (Issue #40).
        stamp = _stable_event_stamp(e, report_date)
        sequence = _event_sequence(e, summary, desc, report_date)

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
