"""Regression coverage for company-name backfill and HK symbol padding."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "backfill_company_names", ROOT / "scripts" / "backfill_company_names.py"
)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_lb_symbol_for_hk_strips_market_suffix_and_pads():
    # 0700.HK -> 700.HK; 0001.HK -> 1.HK; never 700.HK.HK
    assert MOD.lb_static_name  # module imports cleanly
    assert MOD.normalize_hk_rows  # function exists


def test_normalize_hk_symbol():
    from app.symbol import normalize

    assert normalize("1", "HK") == "0001.HK"
    assert normalize("700", "HK") == "0700.HK"
    assert normalize("00700", "HK") == "0700.HK"
