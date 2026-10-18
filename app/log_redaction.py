"""Keep subscription credentials out of log output (Issue #85).

``/ical/{token}`` is the only credential of the iCal feed: ``app/auth.py``
generates it once, ``app/routers/ical.py`` accepts it without any header, and
``scope=all`` returns the whole earnings universe. It still leaked through three
unrelated exits — uvicorn's access log (the request path verbatim), the feed
timeout warning, and provider URLs logged with their query string — while the
repo already declared "no token in a metric label" (``app/schemas.py``) and
shipped an unused ``_sanitize_log_url()``.

This module is the single place those exits share::

    redact_ical_tokens(text)   # "/ical/<token>" -> "/ical/[REDACTED]"
    sanitize_url(url)          # drops a URL's query string, keeps the path
    token_fingerprint(token)   # 8 hex chars, safe to log and to grep
    install_log_redaction()    # attaches the filter to ``uvicorn.access``

Nothing here is allowed to raise on odd input: a logging filter that throws
would silence the log line it was meant to protect, and every caller sits on a
request or provider path.
"""
from __future__ import annotations

import logging
import re
from hashlib import sha256
from typing import Any

REDACTED = "[REDACTED]"

#: ``/ical/`` followed by a credential-shaped segment: ``secrets.token_urlsafe
#: (24)`` yields 32 base64url characters, so requiring 16+ of that alphabet
#: covers every real token while leaving ordinary routes (``/api/me``,
#: ``/assets/app.js``) alone. The run is greedy, so a longer token is consumed
#: whole instead of being partly redacted into a still-readable prefix.
ICAL_TOKEN_IN_PATH = re.compile(r"(/ical/)[A-Za-z0-9_-]{16,}")


def redact_ical_tokens(value: str) -> str:
    """Rewrite every ``/ical/<token>`` segment of ``value`` to ``[REDACTED]``.

    Works on a bare path, on a path with a query string, and on the absolute
    URI form some servers put in a request line.
    """
    if not value:
        return value
    return ICAL_TOKEN_IN_PATH.sub(lambda match: match.group(1) + REDACTED, value)


def sanitize_url(url: str) -> str:
    """Strip the query string from a URL before it reaches a log line.

    Provider endpoints carry their credentials in query parameters
    (``WATCHLIST_HTTP_URL``), and ``urllib``'s ``HTTPError.url`` is the full
    request URL. The path is kept so the log still says *what* was called.
    """
    if not url:
        return url
    head, sep, _query = url.partition("?")
    head = redact_ical_tokens(head)
    return head + "?" + REDACTED if sep else head


def token_fingerprint(token: str | None) -> str:
    """Short, non-reversible identifier for a credential.

    Lets a log line answer "which subscription timed out?" without carrying the
    subscription itself: the operator can compare it to a known token, but a
    leaked log line does not grant access.
    """
    if not token:
        return "-"
    return sha256(token.encode("utf-8")).hexdigest()[:8]


def _redact_args(args: Any) -> Any:
    """Redact every string inside a record's ``args``, preserving its shape.

    ``uvicorn``'s ``AccessFormatter.formatMessage()`` unpacks ``record.args`` as
    the 5-tuple ``(client_addr, method, full_path, http_version, status_code)``,
    so the tuple has to stay a 5-tuple of the same element types — rewriting
    only ``record.getMessage()`` would leave the raw args in place and make the
    formatter raise on every request.
    """
    if isinstance(args, tuple):
        return tuple(_redact_args(item) for item in args)
    if isinstance(args, list):
        return [_redact_args(item) for item in args]
    if isinstance(args, dict):
        return {key: _redact_args(item) for key, item in args.items()}
    if isinstance(args, str):
        return redact_ical_tokens(args)
    return args


class TokenRedactionFilter(logging.Filter):
    """Strip subscription tokens from a record, args included (Issue #85)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        record.args = _redact_args(record.args)
        if isinstance(record.msg, str):
            redacted = redact_ical_tokens(record.msg)
            if redacted != record.msg:
                record.msg = redacted
        return True


def install_log_redaction(logger_name: str = "uvicorn.access") -> logging.Logger:
    """Attach the redaction filter to ``logger_name`` (idempotent).

    A filter belongs to the logger that *emits* the record, so this is installed
    on ``uvicorn.access`` itself rather than on a handler: uvicorn reconfigures
    its handlers at startup, but the logger object (and its filters) survive.
    Repeated calls — app reloads, tests, a second import — must not stack
    filters, hence the isinstance guard.
    """
    logger = logging.getLogger(logger_name)
    if not any(isinstance(existing, TokenRedactionFilter) for existing in logger.filters):
        logger.addFilter(TokenRedactionFilter())
    return logger
