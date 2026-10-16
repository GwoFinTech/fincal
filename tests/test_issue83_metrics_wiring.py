"""Issue #83: the diagnostics counters must have producers.

``/api/admin/diagnostics`` publishes a `cache` section and a `providers`
section (Issue #15). Both were declared but never written: every cache path ran
without touching a counter, and the only instrumented provider entry point
(``provider_client.http_get_json``) is unreachable in the production
configuration. The published OpenAPI/TS contract therefore promised constants —
`hit_rate` 0, `stale_returns` 0, `providers` {} — whatever the traffic.

These tests drive the real decision points with injected fetchers/cursors: no
database, no network, no real clock dependency. They are plain
``unittest.TestCase`` classes so they run under ``pytest`` and under
``python -m unittest`` alike.
"""
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import company_name, config  # noqa: E402
from app.metrics import metrics  # noqa: E402

CACHE_FIELDS = {"hits", "misses", "hit_rate", "stale_returns",
                "refresh_ok", "refresh_fail"}
PROVIDER_ALLOWLIST = {"kurumi", "longbridge", "futu", "cli", "probe-cli"}


class _Resp:
    """Minimal ``urlopen`` context manager returning a JSON body."""

    def __init__(self, payload: bytes = b'{"name": "TENCENT"}'):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeCursor:
    """Cursor whose results are consumed in call order.

    Each test queues exactly what the code under test asks for next; no SQL is
    executed and no connection is opened.
    """

    def __init__(self, queue):
        self._queue = queue

    def execute(self, *args, **kwargs):
        return None

    def _next(self, default):
        return self._queue.pop(0) if self._queue else default

    def fetchone(self):
        return self._next(None)

    def fetchall(self):
        return self._next([])


@contextmanager
def _cursors(queue):
    yield _FakeCursor(queue)


def _cache():
    return metrics.snapshot()["cache"]


def _providers():
    return metrics.snapshot()["providers"]


class CounterIsolationMixin:
    def setUp(self):
        metrics.reset()


class LayerCacheWiringTests(CounterIsolationMixin, TestCase):
    """The four real paths of the earnings layer cache (Issue #83 §1/§3a-b)."""

    def test_cold_start_counts_a_miss(self):
        from app.layer_cache import LayerCache

        cache = LayerCache(default_ttl=120.0, stale_ttl=1800.0)
        cache.get_or_refresh("earnings:probe", lambda: "payload", ttl=120.0)

        self.assertEqual(_cache()["misses"], 1)
        self.assertEqual(_cache()["hits"], 0)
        self.assertEqual(_cache()["refresh_ok"], 1)

    def test_second_read_inside_ttl_counts_a_hit(self):
        from app.layer_cache import LayerCache

        cache = LayerCache(default_ttl=120.0, stale_ttl=1800.0)
        calls = []

        def fetch():
            calls.append(1)
            return "payload"

        cache.get_or_refresh("earnings:probe", fetch, ttl=120.0)
        data, _ = cache.get_or_refresh("earnings:probe", fetch, ttl=120.0)

        self.assertEqual(data, "payload")
        self.assertEqual(len(calls), 1, "the second read must not reach the origin")
        self.assertEqual(_cache()["misses"], 1)
        self.assertEqual(_cache()["hits"], 1)
        self.assertEqual(_cache()["hit_rate"], 0.5)

    def test_stale_while_revalidate_counts_a_stale_return(self):
        from app.layer_cache import LayerCache

        cache = LayerCache(default_ttl=0.05, stale_ttl=1800.0)
        cache.put("k", "old")
        time.sleep(0.08)

        data, _ = cache.get_or_refresh("k", lambda: "new", ttl=0.05)

        self.assertEqual(data, "old")
        self.assertEqual(_cache()["stale_returns"], 1)
        self.assertGreaterEqual(_cache()["hits"], 0)

    def test_failed_background_refresh_counts_refresh_fail(self):
        from app.layer_cache import LayerCache

        cache = LayerCache(default_ttl=0.05, stale_ttl=1800.0)
        cache.put("k", "old")
        time.sleep(0.08)

        def boom():
            raise RuntimeError("upstream down")

        cache.get_or_refresh("k", boom, ttl=0.05)

        deadline = time.time() + 3.0
        while time.time() < deadline and _cache()["refresh_fail"] == 0:
            time.sleep(0.01)
        self.assertEqual(_cache()["refresh_fail"], 1)
        self.assertEqual(_cache()["refresh_ok"], 0)

    def test_blocking_fetch_failure_that_serves_stale_counts_both(self):
        from app.layer_cache import LayerCache

        cache = LayerCache(default_ttl=0.01, stale_ttl=0.01)
        cache.put("k", "old")
        time.sleep(0.03)

        def boom():
            raise RuntimeError("upstream down")

        data, _ = cache.get_or_refresh("k", boom)

        self.assertEqual(data, "old")
        self.assertEqual(_cache()["misses"], 1)
        self.assertEqual(_cache()["refresh_fail"], 1)
        self.assertEqual(_cache()["stale_returns"], 1)

    def test_waiter_joined_onto_a_refresh_counts_a_hit(self):
        """A caller that joins an in-flight refresh cost the origin nothing."""
        from app.layer_cache import LayerCache

        cache = LayerCache(default_ttl=0.2, stale_ttl=0.3, waiter_timeout=2.0)
        cache.put("k", "old")
        time.sleep(0.25)  # stale-valid: the background refresh takes the key

        import threading
        started = threading.Event()

        def slow_fetch():
            started.set()
            time.sleep(0.3)
            return "new"

        cache.get_or_refresh("k", slow_fetch)
        self.assertTrue(started.wait(timeout=2.0))
        time.sleep(0.15)  # fully stale while the refresh still runs
        data, _ = cache.get_or_refresh("k", slow_fetch)

        self.assertEqual(data, "new", "the waiter must receive the refresh's own result")
        self.assertEqual(_cache()["misses"], 0)
        self.assertEqual(_cache()["hits"], 1)


class CacheAttributionTests(CounterIsolationMixin, TestCase):
    """`cache_by_name` attributes the aggregate to the reporting cache."""

    def test_named_cache_reports_to_both_and_the_aggregate_adds_up(self):
        from app.layer_cache import LayerCache

        earnings = LayerCache(default_ttl=120.0, stale_ttl=1800.0, name="earnings")
        ical = LayerCache(default_ttl=120.0, stale_ttl=1800.0, name="ical")

        earnings.get_or_refresh("k", lambda: "a", ttl=120.0)
        earnings.get_or_refresh("k", lambda: "a", ttl=120.0)   # hit
        ical.get_or_refresh("k", lambda: "b", ttl=120.0)

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["cache_by_name"]["earnings"]["misses"], 1)
        self.assertEqual(snapshot["cache_by_name"]["earnings"]["hits"], 1)
        self.assertEqual(snapshot["cache_by_name"]["ical"]["misses"], 1)
        self.assertEqual(snapshot["cache_by_name"]["ical"]["hits"], 0)
        self.assertEqual(snapshot["cache"]["hits"],
                         sum(entry["hits"] for entry in snapshot["cache_by_name"].values()))
        self.assertEqual(snapshot["cache"]["misses"],
                         sum(entry["misses"] for entry in snapshot["cache_by_name"].values()))

    def test_unnamed_cache_does_not_invent_a_label(self):
        from app.layer_cache import LayerCache

        LayerCache(default_ttl=120.0).get_or_refresh("k", lambda: "a", ttl=120.0)

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["cache"]["misses"], 1)
        self.assertEqual(snapshot["cache_by_name"], {})

    def test_endpoint_caches_are_attributed_to_their_own_names(self):
        """The two production caches report under constants, not their keys."""
        from app.routers import ical
        from app.routers.api import _earnings_cache

        self.assertEqual(_earnings_cache._name, "earnings")
        self.assertEqual(ical.FEED_CACHE_NAME, "ical")

        _earnings_cache.get_or_refresh("earnings:k", lambda: "a", ttl=120.0)
        metrics.record_cache_hit(ical.FEED_CACHE_NAME)

        snapshot = metrics.snapshot()
        self.assertEqual(set(snapshot["cache_by_name"]), {"earnings", "ical"})
        self.assertEqual(snapshot["cache"]["hits"], 1)
        self.assertEqual(snapshot["cache"]["misses"], 1)


class EarningsEndpointWiringTests(CounterIsolationMixin, TestCase):
    """Acceptance #1 over the endpoint itself, not a stand-in for it."""

    def _call(self):
        from app.routers import api

        queue = [[]]  # SELECT symbol, market FROM watchlist → no rows
        with mock.patch.object(api, "ensure_user",
                               return_value={"id": 7, "email": "u@example.com", "name": "u"}), \
                mock.patch("app.universe.popular_stocks", return_value=(["AAPL"], [])), \
                mock.patch("app.db.db_cursor", lambda: _cursors(queue)), \
                mock.patch("app.earnings.fetch_earnings_from_db",
                           return_value=[{"symbol": "AAPL", "market": "US"}]):
            return api.api_earnings(start=None, end=None, watchlistOnly=False,
                                    user={"id": 7, "email": "u@example.com", "name": "u"})

    def setUp(self):
        super().setUp()
        from app.routers import api
        api._earnings_cache.invalidate()

    def test_cold_then_warm_request_moves_both_counters(self):
        self.assertEqual(self._call(), [{"symbol": "AAPL", "market": "US"}])
        self._call()

        self.assertEqual(_cache()["misses"], 1)
        self.assertEqual(_cache()["hits"], 1)
        self.assertEqual(_cache()["refresh_ok"], 1)


class ICalCacheWiringTests(CounterIsolationMixin, TestCase):
    """Acceptance #5: the most frequently polled entry point is measurable."""

    def setUp(self):
        super().setUp()
        from app.routers import ical
        ical.invalidate_ical_cache()

    def _fake_request(self, etag=None):
        headers = {} if etag is None else {"if-none-match": etag}
        return SimpleNamespace(headers=headers)

    def _call(self, token="sub-token", request=None):
        from app.routers import ical

        queue = [{"id": 7, "email": "u@example.com", "name": "u"},
                 [{"symbol": "AAPL", "market": "US"}]]
        with mock.patch("app.db.db_cursor", lambda: _cursors(queue)), \
                mock.patch("app.earnings.fetch_earnings_from_db", return_value=[]):
            return ical.ical_feed(request=request or self._fake_request(),
                                  token=token, lang="zh", scope="watchlist",
                                  predicted=1, markets="all")

    def test_cold_request_is_a_miss_and_second_is_a_hit(self):
        first = self._call()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(_cache()["misses"], 1)
        self.assertEqual(_cache()["hits"], 0)

        second = self._call()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(_cache()["misses"], 1)
        self.assertEqual(_cache()["hits"], 1)

    def test_conditional_request_on_a_cached_feed_is_still_a_hit(self):
        first = self._call()
        etag = first.headers["etag"]

        not_modified = self._call(request=self._fake_request(etag=etag))

        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(_cache()["hits"], 1)
        self.assertEqual(_cache()["misses"], 1)

    def test_unknown_token_is_not_counted_as_a_miss(self):
        from app.routers import ical

        with mock.patch("app.db.db_cursor", lambda: _cursors([None])):
            response = ical.ical_feed(request=self._fake_request(), token="nope",
                                      lang="zh", scope="watchlist", predicted=1,
                                      markets="all")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(_cache()["misses"], 0)
        self.assertEqual(_cache()["hits"], 0)


class ProviderWiringTests(CounterIsolationMixin, TestCase):
    """Acceptance #4: kurumi (health probe included), longbridge and futu."""

    def _patch_kurumi_base(self):
        return mock.patch.object(config, "KURUMI_API_URL", "http://kurumi.internal:9000")

    def test_kurumi_success_is_counted(self):
        with self._patch_kurumi_base(), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(return_value=_Resp())):
            name = company_name.fetch_from_kurumi("0700.HK", "HK")

        self.assertEqual(name, "TENCENT")
        self.assertEqual(_providers()["kurumi"]["calls"], 1)
        self.assertEqual(_providers()["kurumi"]["success"], 1)

    def test_kurumi_failure_is_counted_with_its_category(self):
        refused = URLError(ConnectionRefusedError(111, "refused"))
        with self._patch_kurumi_base(), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(side_effect=refused)):
            with self.assertRaises(Exception):
                company_name.fetch_from_kurumi("0700.HK", "HK")

        stats = _providers()["kurumi"]
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["success"], 0)
        self.assertEqual(stats["errors"]["connection"], 1)

    def test_health_probe_counts_its_real_upstream_call(self):
        """`/api/admin/health` polls Kurumi every time; that call must show up."""
        with self._patch_kurumi_base(), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(return_value=_Resp())):
            verdict = company_name.probe_kurumi()

        self.assertEqual(verdict, {"status": "healthy"})
        self.assertEqual(_providers()["kurumi"]["calls"], 1)
        self.assertEqual(_providers()["kurumi"]["success"], 1)

    def test_longbridge_cli_is_counted(self):
        ok = SimpleNamespace(returncode=0, stdout=json.dumps([{"name": "TENCENT"}]))
        with mock.patch("subprocess.run", return_value=ok):
            name = company_name.fetch_from_longbridge("0700.HK", "HK")

        self.assertEqual(name, "TENCENT")
        self.assertEqual(_providers()["longbridge"]["calls"], 1)
        self.assertEqual(_providers()["longbridge"]["success"], 1)

        failed = SimpleNamespace(returncode=2, stdout="", stderr="boom")
        with mock.patch("subprocess.run", return_value=failed):
            with self.assertRaises(Exception):
                company_name.fetch_from_longbridge("0700.HK", "HK")

        stats = _providers()["longbridge"]
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["success"], 1)

    def _install_fake_futu(self, ret, name="Tencent Holdings"):
        from types import SimpleNamespace as NS
        import futu as futu_mod

        class _Row:
            def get(self, key, default=None):
                return name if key == "name" else default

        class _Data:
            empty = False
            iloc = [_Row()]

        class _FakeCtx:
            def __init__(self, host, port):
                pass

            def get_stock_basicinfo(self, market, sec_type, code_list):
                return (ret, _Data() if ret == 0 else "OpenD error")

            def close(self):
                pass

        patch = mock.patch.multiple(
            futu_mod,
            RET_OK=0,
            Market=NS(HK="HK", US="US"),
            SecurityType=NS(STOCK="STOCK"),
            OpenQuoteContext=_FakeCtx,
        )
        return patch

    def test_futu_success_is_counted(self):
        with self._install_fake_futu(ret=0):
            self.assertEqual(company_name.fetch_from_futu("0700.HK", "HK"),
                             "Tencent Holdings")

        self.assertEqual(_providers()["futu"]["calls"], 1)
        self.assertEqual(_providers()["futu"]["success"], 1)

    def test_futu_error_reply_is_counted_as_a_failure(self):
        with self._install_fake_futu(ret=1):
            self.assertEqual(company_name.fetch_from_futu("0700.HK", "HK"), "")

        stats = _providers()["futu"]
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["success"], 0)
        self.assertEqual(stats["errors"]["invalid_response"], 1)

    def test_cli_json_helper_is_counted_too(self):
        from app.provider_client import ProviderConfig, cli_json

        cfg = ProviderConfig(name="probe-cli", max_retries=0)
        ok = SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True}))
        with mock.patch("subprocess.run", return_value=ok):
            self.assertEqual(cli_json(["probe"], cfg=cfg), {"ok": True})
        self.assertEqual(_providers()["probe-cli"]["success"], 1)

        junk = SimpleNamespace(returncode=0, stdout="not json")
        with mock.patch("subprocess.run", return_value=junk):
            with self.assertRaises(Exception):
                cli_json(["probe"], cfg=cfg)
        stats = _providers()["probe-cli"]
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["errors"]["invalid_response"], 1)

    def test_labels_never_carry_a_symbol_token_or_user(self):
        """Low cardinality: the label set is fixed by code, not by traffic."""
        with self._patch_kurumi_base(), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(return_value=_Resp())), \
                mock.patch("subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps([{"name": "TENCENT"}]))):
            company_name.fetch_from_kurumi("0700.HK", "HK")
            company_name.fetch_from_longbridge("0700.HK", "HK")

        snapshot = metrics.snapshot()
        self.assertLessEqual(set(snapshot["providers"]), PROVIDER_ALLOWLIST)
        # The cache counters are aggregate values plus one bucket per named
        # cache: there is nowhere for a cache key, token or user id to end up.
        self.assertEqual(set(snapshot["cache"]), CACHE_FIELDS)
        self.assertLessEqual(set(snapshot["cache_by_name"]), {"earnings", "ical"})


class ProducerSourceTests(TestCase):
    """The static half of the issue: every counter has a call site."""

    def _source(self, relative):
        return (ROOT / relative).read_text()

    def test_cache_counters_are_written_at_the_decision_points(self):
        layer_cache = self._source("app/layer_cache.py")
        for call in ("record_cache_hit(self._name)", "record_cache_miss(self._name)",
                     "record_cache_stale(self._name)",
                     "record_cache_refresh(True, self._name)",
                     "record_cache_refresh(False, self._name)"):
            self.assertIn(call, layer_cache, f"layer cache never reports {call}")

        ical = self._source("app/routers/ical.py")
        self.assertIn("record_cache_hit(FEED_CACHE_NAME)", ical)
        self.assertIn("record_cache_miss(FEED_CACHE_NAME)", ical)
        # The label is a module-level constant, not the per-token cache key.
        self.assertIn('FEED_CACHE_NAME = "ical"', ical)

    def test_provider_counters_are_written_for_every_client_path(self):
        self.assertIn("record_provider_call",
                      self._source("app/provider_client.py"))
        company = self._source("app/company_name.py")
        self.assertIn("_count_call(", company)
        for provider in ('"kurumi"', '"longbridge"', '"futu"'):
            self.assertIn(provider, company)

    def test_no_per_request_value_becomes_a_metric_label(self):
        """A cache key, token or symbol must never be passed to a counter."""
        for relative in ("app/layer_cache.py", "app/routers/ical.py",
                         "app/company_name.py", "app/provider_client.py"):
            source = self._source(relative)
            for line in source.splitlines():
                if "record_cache_" in line or "_count_call(" in line:
                    self.assertNotIn("cache_key", line, f"{relative}: {line.strip()}")
                    self.assertNotIn("token", line, f"{relative}: {line.strip()}")
                    self.assertNotIn("symbol", line, f"{relative}: {line.strip()}")
