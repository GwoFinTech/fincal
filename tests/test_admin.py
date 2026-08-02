"""Admin authorization and global watchlist normalization regression tests."""
import sys
from pathlib import Path
from unittest import TestCase

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth import require_admin
from app.admin_watchlist import normalize_managed_symbol


class AdminAuthorizationTests(TestCase):
    def test_admin_role_is_accepted(self):
        user = {"id": 1, "role": "admin"}
        self.assertIs(require_admin(user), user)

    def test_non_admin_role_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            require_admin({"id": 1, "role": "user"})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "admin_required")


class ManagedWatchlistSymbolTests(TestCase):
    def test_us_and_hk_symbols_use_database_canonical_form(self):
        self.assertEqual(normalize_managed_symbol("aapl.us", "US"), ("AAPL", "US"))
        self.assertEqual(normalize_managed_symbol("700", "HK"), ("0700.HK", "HK"))

    def test_unsupported_market_returns_language_neutral_error_code(self):
        with self.assertRaisesRegex(ValueError, "market_unsupported"):
            normalize_managed_symbol("600519", "CN")
