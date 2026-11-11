"""Issue #119: the SPA shell must load nothing from a third party.

`app/static/index.html` is the whole product UI. It used to pull Inter from
`fonts.googleapis.com` through a `<link rel="stylesheet">` in `<head>` — render
blocking, so with that host blackholed the page produced no DOM for 180s (the
`deploy.sh` gate called fonts "progressive enhancement" and let it through;
`display=swap` only governs when a *loaded* font applies). Measured A/B, see the
issue.

These checks are structural, so the regression is caught at test time instead of
by a blank page in a network without egress:

* every subresource the shell references (`<link href>`, `<script src>`,
  `<img src>`) resolves inside the origin;
* the deploy gate covers the font hosts, not just unpkg / cdn.tailwindcss — the
  gate's own regex is read out of `scripts/deploy.sh` and exercised here, so
  narrowing it again fails a test rather than a deploy;
* the font stack still degrades to a system CJK family now that Inter is no
  longer fetched.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "app" / "static" / "index.html"
DEPLOY_SH = ROOT / "scripts" / "deploy.sh"

# Hosts whose runtime assets must never be part of the first paint.
THIRD_PARTY_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "unpkg.com",
    "cdn.tailwindcss.com",
)

SUBRESOURCE_ATTRS = {
    "link": "href",
    "script": "src",
    "img": "src",
    "iframe": "src",
    "source": "src",
}


class _SubresourceCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_name = SUBRESOURCE_ATTRS.get(tag)
        if attr_name is None:
            return
        for name, value in attrs:
            if name == attr_name and value:
                self.found.append((tag, value))

    handle_startendtag = handle_starttag


def _subresources(html: str) -> list[tuple[str, str]]:
    collector = _SubresourceCollector()
    collector.feed(html)
    return collector.found


def _deploy_gate_pattern() -> re.Pattern[str]:
    """The regex `scripts/deploy.sh` greps index.html with."""
    script = DEPLOY_SH.read_text(encoding="utf-8")
    match = re.search(r"grep -qE '([^']+)' \"?\$INDEX_HTML\"?", script)
    assert match, "frontend asset gate not found in scripts/deploy.sh"
    return re.compile(match.group(1))


def test_shell_references_no_cross_origin_subresources():
    html = INDEX_HTML.read_text(encoding="utf-8")

    external = []
    for tag, url in _subresources(html):
        parsed = urlparse(url)
        if parsed.scheme or parsed.netloc or url.startswith("//"):
            external.append(f"<{tag}> {url}")

    assert external == [], (
        "index.html loads subresources from outside the origin; a stylesheet or "
        "script in <head> blocks first paint when it is unreachable (Issue #119): "
        + ", ".join(external)
    )


def test_no_third_party_host_appears_in_the_shell_at_all():
    """Stronger than the gate: not even a preconnect/dns-prefetch hint, which
    would still ship the visitor's IP to the third party."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    hits = [host for host in THIRD_PARTY_HOSTS if host in html]
    assert hits == [], f"index.html still references third-party hosts: {hits}"


def test_font_stack_still_falls_back_to_a_system_cjk_family():
    """Inter is no longer downloaded, so the stack must not depend on it."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r"font-family:\s*([^;}]+);", html)
    assert match, "the base font stack is gone from index.html"
    stack = [part.strip().strip("'\"") for part in match.group(1).split(",")]

    assert "system-ui" in stack, stack
    assert any("Noto Sans CJK" in family for family in stack), stack
    # Inter may stay as a locally installed option, but it must not be the only
    # family: the stack has to render CJK without it.
    without_inter = [family for family in stack if family != "Inter"]
    assert len(without_inter) >= 3, without_inter


def test_deploy_gate_rejects_every_third_party_host():
    """The gate must cover fonts.googleapis.com, not only the CDNs #42 removed."""
    gate = _deploy_gate_pattern()

    for host in THIRD_PARTY_HOSTS:
        sample = f'  <link href="https://{host}/asset.css" rel="stylesheet">'
        assert gate.search(sample), f"deploy.sh gate does not reject {host}"

    # ... and it must not fire on the committed local assets.
    for local in (
        '  <link rel="stylesheet" href="/assets/tailwind.css">',
        '  <script src="/assets/vendor/vue.global.prod.js"></script>',
    ):
        assert not gate.search(local), f"deploy.sh gate rejects a local asset: {local}"


def test_deploy_gate_fires_on_the_real_file_when_fonts_return():
    """End-to-end on the gate's own logic: re-adding the removed lines is caught.

    `deploy.sh` is a bash script with an rsync/rebuild tail, so the assertion is
    made against its grep, not by running the deploy.
    """
    gate = _deploy_gate_pattern()
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert not gate.search(html)

    regressed = html.replace(
        '  <script src="/assets/vendor/vue.global.prod.js"></script>',
        '  <script src="/assets/vendor/vue.global.prod.js"></script>\n'
        '  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400&display=swap" rel="stylesheet">',
        1,
    )
    assert regressed != html, "anchor line for the regression probe is missing"
    assert gate.search(regressed), "re-adding the fonts stylesheet no longer trips the gate"
