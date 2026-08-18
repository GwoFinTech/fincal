"""Regression tests for predict_earnings merge_duplicate_symbols (Issue #2).

Verifies 5-digit HK codes are normalized to 4-digit canonical using
app.symbol.normalize — the single source of truth.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.symbol import normalize  # noqa: E402


def test_five_digit_hk_normalizes_to_four_digit():
    """00700.HK → 0700.HK, 00005.HK → 0005.HK"""
    assert normalize("00700", "HK") == "0700.HK"
    assert normalize("00005", "HK") == "0005.HK"
    assert normalize("00001", "HK") == "0001.HK"
    assert normalize("09988", "HK") == "9988.HK"


def test_four_digit_hk_unchanged():
    """Already canonical codes stay canonical."""
    assert normalize("0700", "HK") == "0700.HK"
    assert normalize("0005", "HK") == "0005.HK"
    assert normalize("9988", "HK") == "9988.HK"


def test_two_and_three_digit_hk_pad_to_four():
    """Short codes are zero-padded."""
    assert normalize("700", "HK") == "0700.HK"
    assert normalize("5", "HK") == "0005.HK"
    assert normalize("1", "HK") == "0001.HK"


def test_old_lstrip_8_would_have_broken():
    """Demonstrate the old bug: lstrip('8') corrupts codes starting with 8.

    Old logic: (sym.split('.')[0].lstrip('8') or '0').zfill(4) + '.HK'
    For 00823.HK → lstrip('8') on '00823' → '00823' (no change, '8' not leading)
    But for 80000.HK → lstrip('8') on '80000' → '000' → zfill(4) → '0000' → '0000.HK'

    The new normalize() handles all cases correctly.
    """
    # 80000 is a valid HK code (e.g. HSI futures proxy)
    assert normalize("80000", "HK") == "80000.HK"
    # Old code would have produced '0000.HK' — wrong!
    old_result = ("80000".lstrip("8") or "0").zfill(4) + ".HK"
    assert old_result == "0000.HK"  # proves the bug existed
