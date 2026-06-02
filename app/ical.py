"""iCal (.ics) feed generation for user watchlist."""
import uuid
from datetime import date, datetime
from . import config


def generate_ical(earnings: list[dict], user_email: str = "") -> str:
    """Generate iCal content from earnings records."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FinCal//Earnings Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:FinCal Earnings",
        "X-WR-TIMEZONE:Asia/Hong_Kong",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]

    for e in earnings:
        report_date = e.get("report_date")
        if isinstance(report_date, datetime):
            report_date = report_date.date()
        if not report_date:
            continue

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
        summary = f"{symbol} ({market}) {fq_str} Earnings{pred_marker}"
        desc_parts = [f"Company: {company}" if company else "",
                      f"Fiscal: FY{fy} {fq_str}" if fy else "",
                      f"Timing: {time_label}" if time_label else "",
                      f"⚠ Predicted date (not confirmed)" if is_pred else "",
                      f"EPS Est: {e.get('eps_estimate')}" if e.get('eps_estimate') else "",
                      f"EPS Actual: {e.get('eps_actual')}" if e.get('eps_actual') else ""]
        desc = "\\\\n".join(p for p in desc_parts if p)

        dt_str = report_date.strftime("%Y%m%d")
        uid = f"fincal-{symbol}-{market}-{dt_str}-{uuid.uuid4().hex[:8]}@{config.APP_NAME}"

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"SUMMARY:{summary}")
        lines.append(f"DESCRIPTION:{desc}")

        if hour_start:
            lines.append(f"DTSTART;TZID=America/New_York:{dt_str}T{hour_start}")
            lines.append(f"DTEND;TZID=America/New_York:{dt_str}T{hour_end}")
        else:
            lines.append(f"DTSTART;VALUE=DATE:{dt_str}")
            lines.append(f"DTEND;VALUE=DATE:{dt_str}")

        lines.append(f"STATUS:{'TENTATIVE' if is_pred else 'CONFIRMED'}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
