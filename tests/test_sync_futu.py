"""Regression tests for optional Futu OpenD startup behavior."""
import importlib.util
import socket
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location("sync_futu", ROOT / "scripts" / "sync_futu.py")
sync_futu = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_futu)


class FutuContextStartupTests(TestCase):
    def test_unavailable_opend_skips_without_constructing_retrying_context(self):
        with patch.object(sync_futu.socket, "create_connection", side_effect=ConnectionRefusedError("refused")) as connect:
            with patch.dict(sys.modules, {"futu": MagicMock()}):
                self.assertIsNone(sync_futu.create_futu_context())

        connect.assert_called_once_with(
            (sync_futu.config.FUTU_HOST, sync_futu.config.FUTU_PORT), timeout=3
        )

    def test_available_opend_uses_configured_endpoint(self):
        connection = MagicMock()
        context = MagicMock()
        futu_module = MagicMock()
        futu_module.OpenQuoteContext.return_value = context
        with patch.object(sync_futu.socket, "create_connection", return_value=connection):
            with patch.dict(sys.modules, {"futu": futu_module}):
                self.assertIs(sync_futu.create_futu_context(), context)

        connection.__enter__.assert_called_once()
        futu_module.OpenQuoteContext.assert_called_once_with(
            host=sync_futu.config.FUTU_HOST, port=sync_futu.config.FUTU_PORT
        )


class EarningsSymbolTests(TestCase):
    def test_us_watchlist_suffix_is_not_written_to_earnings_key(self):
        self.assertEqual(sync_futu.canonical_earnings_symbol("aapl.us"), ("AAPL", "US"))

    def test_hk_symbol_keeps_four_digit_earnings_convention(self):
        self.assertEqual(sync_futu.canonical_earnings_symbol("700.hk"), ("0700.HK", "HK"))

    def test_unknown_market_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported_market:CN"):
            sync_futu.canonical_earnings_symbol("600519.cn")
