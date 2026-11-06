"""Keep subscription credentials out of log output (Issue #85).

``/ical/{token}`` is the only credential of the iCal feed: ``app/auth.py``
generates it once, ``app/routers/ical.py`` accepts it without any header, and
``scope=all`` returns the whole system universe. It still leaked through three
unrelated exits — uvicorn's access log (the request path verbatim), the feed
timeout warning, and provider URLs logged with their query string — while the
repo already declared "no token in a metric label" (``app/schemas.py``) and
shipped an unused ``_sanitize_log_url()``.

This module is the single place those exits share::

    redact_ical_tokens(text)   # "/ical/<token>" -> "/ical/[REDACTED]"
    sanitize_url(url)          # drops a URL's query string, keeps the path
    token_fingerprint(token)   # 8 hex chars, safe to log and to grep
    install_log_redaction()    # attaches the filter to every exit a record takes

Nothing here is allowed to raise on odd input: a logging filter that throws
would silence the log line it was meant to protect, and every caller sits on a
request or provider path.

A logger-level ``Filter`` only sees records emitted by *that* logger: a child
logger such as ``app.errors`` propagates to the root handlers without passing
through any ancestor logger's filters, so a handler that has no filter prints
the record verbatim. ``install_log_redaction()`` therefore installs the filter
on the root handlers as well (Issue #114) — that is the layer every propagated
record must cross, whoever wrote it.
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


def _attach_to_handler(handler: logging.Handler) -> bool:
    """Put the filter on one handler; ``True`` when it was added.

    ``Handler.handle()`` runs this filter for every record that reaches the
    handler, whatever logger emitted it — that is the point (Issue #114): a
    record emitted by ``app.errors`` propagates straight to the root handler and
    never crosses a logger-level filter on the way. Returns rather than asserts
    so a caller can report what it changed, and the isinstance check keeps the
    install idempotent when it runs again (tests, reloads, a second import).
    """
    if any(isinstance(existing, TokenRedactionFilter) for existing in handler.filters):
        return False
    handler.addFilter(TokenRedactionFilter())
    return True


def install_log_redaction(logger_name: str = "uvicorn.access") -> logging.Logger:
    """Attach the redaction filter to every exit a record can take (idempotent).

    Two layers, because they cover different records:

    * ``logger_name`` (``uvicorn.access``) — uvicorn's own access lines are
      emitted there and that logger's handler is configured by uvicorn, which
      re-runs its log config at startup; the logger object (and its filters)
      survive that, the handler does not have to.
    * every root handler — the layer every *propagated* record crosses. An
      application logger (``app.errors`` and anything below ``app.``) has no
      handler of its own, so its record goes to the root handler unfiltered
      unless that handler carries the filter (#114). The root handler itself is
      created by ``logging.basicConfig`` in ``app.main``, before this runs.

    Repeatable by contract: called on import, on a reload, from a test — it adds
    nothing twice. A handler attached *after* this call is the documented
    residual risk; calling again covers it.
    """
    logger = logging.getLogger(logger_name)
    if not any(isinstance(existing, TokenRedactionFilter) for existing in logger.filters):
        logger.addFilter(TokenRedactionFilter())
    for handler in logging.getLogger().handlers:
        _attach_to_handler(handler)
    return logger
