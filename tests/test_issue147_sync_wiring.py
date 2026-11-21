"""Issue #147 的接线守卫：`sync_actuals` 真的把 pairs 写进去了吗？

为什么需要这条：第一版 hook 传的是 `client`，而 `sync_actuals` 的客户端参数叫 `ctx`。
NameError 被「enrichment 不能弄红阶段」的 wide `except` 吞掉，于是
- 单元测试全绿（没人跑完整阶段），
- 生产同步跑完、日志里只有一行 warning，
- `earnings_provider_pairs` 是 0 行。

所以这里跑完整的 `sync_actuals`，同时钉住两件事：写进去了，以及 `pair_errors == 0`
（异常计数存在，就是为了让「静默失效」变成可观测的数字）。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.sync_futu as sync_futu  # noqa: E402


class _RecordingCursor:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _income_response():
    return {"report_list": [{
        "fiscal_year": 2026, "financial_type": 3,
        "currency_code": "CNY", "accounting_standards": "IAS",
        "item_list": [
            {"field_id": 54, "display_name": "Basic EPS", "data": 6.947, "yoy": 1.5},
            {"field_id": 1, "display_name": "Total Revenue", "data": 207336000000.0,
             "yoy": 7.5},
        ],
    }]}


def _estimate_response():
    return (0, {"currency": "CNY", "period_type": "QUARTERLY",
                "history": [{"fiscal_year": 2026, "fiscal_quarter": 3,
                             "consensus": 6.2873, "actual": 6.947, "num_analysts": 7,
                             "period_str": "2026/Q3"}],
                "future": []})


def _stats():
    return sync_futu.FutuStageStats()


def test_sync_actuals_writes_the_provider_pairs():
    """The whole stage, not just the helper: the hook has to reach the writer."""
    ctx = MagicMock()
    ctx.get_financials_statements.return_value = (0, _income_response())
    ctx.get_financials_estimate.return_value = _estimate_response()
    cursor = _RecordingCursor()
    stats = _stats()

    with patch.object(sync_futu, "check_cancelled"), \
         patch.object(sync_futu, "db_cursor", lambda: cursor), \
         patch.object(sync_futu, "get_rate_limiter", return_value=_FastLimiter()), \
         patch.object(sync_futu, "HeartbeatThrottle", _NoHeartbeat):
        sync_futu.sync_actuals(ctx, 1, ["0700.HK"], stats=stats)

    pairs = [params for sql, params in cursor.executed
             if "earnings_provider_pairs" in sql]
    assert len(pairs) == 1, (
        "sync_actuals did not write the provider pair — the hook most likely names a "
        f"client the loop never binds (executed {len(cursor.executed)} statement(s))")
    assert pairs[0][:4] == ("0700.HK", "HK", 2026, 3)
    assert pairs[0][4] == "CNY" and pairs[0][6] == pytest.approx(6.947)
    assert stats.pair_rows == 1 and stats.pair_symbols == 1
    # The counter that makes a silent outage visible.
    assert stats.pair_errors == 0


def test_a_raising_estimate_read_is_counted_not_swallowed_silently():
    ctx = MagicMock()
    ctx.get_financials_statements.return_value = (0, _income_response())
    ctx.get_financials_estimate.side_effect = RuntimeError("boom")
    cursor = _RecordingCursor()
    stats = _stats()

    with patch.object(sync_futu, "check_cancelled"), \
         patch.object(sync_futu, "db_cursor", lambda: cursor), \
         patch.object(sync_futu, "get_rate_limiter", return_value=_FastLimiter()), \
         patch.object(sync_futu, "HeartbeatThrottle", _NoHeartbeat):
        sync_futu.sync_actuals(ctx, 1, ["0700.HK"], stats=stats)

    assert stats.pair_errors == 1
    assert stats.pair_rows == 0
    # The statements half still landed: enrichment never blocks the actuals.
    assert any(sql.strip().startswith("UPDATE earnings") for sql, _ in cursor.executed)


def test_an_unsupported_symbol_is_counted_separately():
    ctx = MagicMock()
    ctx.get_financials_statements.return_value = (0, _income_response())
    ctx.get_financials_estimate.return_value = (0, {"currency": None, "history": [],
                                                    "future": [], "period_type": "QUARTERLY"})
    cursor = _RecordingCursor()
    stats = _stats()

    with patch.object(sync_futu, "check_cancelled"), \
         patch.object(sync_futu, "db_cursor", lambda: cursor), \
         patch.object(sync_futu, "get_rate_limiter", return_value=_FastLimiter()), \
         patch.object(sync_futu, "HeartbeatThrottle", _NoHeartbeat):
        sync_futu.sync_actuals(ctx, 1, ["0700.HK"], stats=stats)

    assert stats.pair_errors == 0 and stats.pair_rows == 0


class _FastLimiter:
    window_seconds = 30.0

    def acquire(self):
        return 0.0

    def cool_down(self):
        pass


class _NoHeartbeat:
    def maybe(self, *args, **kwargs):
        return None
