"""Issue #135: the README's compression section must describe the real entry point.

It claimed `GZipMiddleware(minimum_size=1024, compresslevel=6)`, but Issue #99
replaced Starlette's stock middleware with `app.compression.CompressedResponses`
(mounted in `app/main.py`). Anyone — or any automated job — reading the README
went looking for a class that is not in the request path, and the behaviour that
#99 actually introduced (buffer until the size is known, weaken strong ETags,
never buffer `text/event-stream`) was documented nowhere but in the code.

These tests bind the prose to the code: paths must exist, and the numbers must be
the ones `app/main.py` mounts.
"""
import re
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _compression_section() -> str:
    text = _readme()
    start = text.index("**Compression (Issue #99).**")
    return text[start:text.index("\n## ", start)]


def _main_py() -> str:
    return (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def _compression_py() -> str:
    return (ROOT / "app" / "compression.py").read_text(encoding="utf-8")


class ReadmeCompressionTests(TestCase):
    def test_the_named_entry_point_exists_and_is_mounted(self):
        section = _compression_section()
        assert "app.compression.CompressedResponses" in section
        assert (ROOT / "app" / "compression.py").exists()
        assert "class CompressedResponses" in _compression_py()
        assert "add_middleware(CompressedResponses" in _main_py(), "the class the README names must be the one in use"

    def test_the_stock_middleware_is_not_claimed_as_the_entry_point(self):
        section = _compression_section()
        # Mentioning it is fine — claiming it is the implementation is not.
        assert "**not** by Starlette's `GZipMiddleware`" in section
        assert not re.search(r"gzipped by the application\s*\(`GZipMiddleware`", section)

    def test_the_documented_numbers_are_the_mounted_ones(self):
        section = _compression_section()
        mounted = re.search(r"add_middleware\(CompressedResponses,\s*minimum_size=(\d+),\s*compresslevel=(\d+)\)", _main_py())
        assert mounted, "could not read the middleware mount from app/main.py"
        minimum_size, compresslevel = mounted.groups()
        assert f"minimum_size={minimum_size}, compresslevel={compresslevel}" in section, (
            "the README's threshold/level must match app/main.py"
        )
        assert f"`minimum_size` ({minimum_size} B)" in section or f"({minimum_size} B)" in section

    def test_the_documented_exclusion_matches_the_code(self):
        excluded = re.search(r"_EXCLUDED_CONTENT_TYPES = \(([^)]*)\)", _compression_py()).group(1)
        for content_type in re.findall(r'"([^"]+)"', excluded):
            assert content_type in _compression_section(), (
                f"the module excludes {content_type}; the README must say so"
            )

    def test_the_documented_buffering_behaviour_matches_the_code(self):
        code = _compression_py()
        assert "self.pending_size < self.minimum_size" in code
        section = _compression_section()
        assert "buffers until the body is known to be at least" in section
        assert "identity" in section

    def test_the_documented_validator_rule_matches_the_code(self):
        assert "def _weaken(" in _compression_py()
        assert 'W/"…"' in _compression_section()

    def test_every_repo_path_named_in_the_section_exists(self):
        for path in re.findall(r"`(app/[\w/]+\.py)`", _compression_section()):
            assert (ROOT / path).exists(), f"README references a missing file: {path}"

    def test_the_traefik_warning_survives(self):
        assert "Do **not** also add Traefik" in _compression_section(), (
            "the edge-compression warning is the reason a future reader does not re-add it"
        )
