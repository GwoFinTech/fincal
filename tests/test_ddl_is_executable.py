"""`init_db()` 的 DDL 必须能被 Postgres 执行——`#` 注释会让容器启动即崩。

Issue #147 的真实教训：给 `earnings_provider_pairs` 建表时把解释性注释写成 Python 风格
的 `#`，而它落在 `cur.execute(\"\"\"…\"\"\")` 的 SQL 字面量里。离线测试全绿（没人把 DDL 真的
跑一遍），部署后 `Application startup failed: syntax error at or near "#"`，容器进入重启循环。
SQL 只认 `--`，所以这里在测试层拦住这一类错误：所有 DDL 块不得出现 `#`（注释或字面量），
并确认本 issue 的表名确实在其中。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_SOURCE = ROOT / "app" / "db.py"

_DDL_BLOCK = re.compile(r'cur\.execute\(\s*f?"""(.*?)"""', re.S)


def _ddl_blocks() -> list[str]:
    return _DDL_BLOCK.findall(DB_SOURCE.read_text(encoding="utf-8"))


def test_the_source_really_holds_ddl_blocks():
    """A parser that silently matched nothing would make the guard below vacuous."""
    blocks = _ddl_blocks()
    assert len(blocks) >= 10, f"only {len(blocks)} DDL block(s) parsed out of app/db.py"
    assert any("CREATE TABLE IF NOT EXISTS earnings (" in block for block in blocks)


def test_no_ddl_block_carries_a_hash_comment():
    offenders = []
    for block in _ddl_blocks():
        for line in block.splitlines():
            if "#" in line:
                offenders.append(line.strip())
    assert offenders == [], (
        "SQL only knows `--`; a `#` inside a DDL literal fails at startup, not in "
        f"pytest: {offenders}"
    )


def test_no_ddl_block_carries_a_python_style_indent_comment():
    """The other half of the same mistake: a `#` paragraph pasted inside the SQL."""
    for block in _ddl_blocks():
        assert not re.search(r"(?m)^\s*#", block), block[:120]


def test_the_provider_metric_table_is_created():
    """Issues #147/#148: the read path relies on this table existing after init_db()."""
    blocks = "\n".join(_ddl_blocks())
    assert "CREATE TABLE IF NOT EXISTS earnings_provider_metrics" in blocks
    # ``metric`` is part of the key: one row per period *per metric*, because the
    # currency is a property of the metric (TSM: revenue TWD, EPS consensus USD).
    assert ("PRIMARY KEY (symbol, market, fiscal_year, fiscal_quarter, source, metric)"
            in blocks)
    # The pair must be able to hold the provider's own actual next to its consensus —
    # that adjacency is the whole point (one provider, one currency, one unit).
    for column in ("metric", "currency", "consensus", "actual", "num_analysts", "as_of"):
        assert column in blocks


def test_the_eps_only_predecessor_is_folded_in_and_dropped():
    """#148 replaced ``earnings_provider_pairs``; a metric-aware table must not leave
    the old one behind as a second source of truth."""
    blocks = "\n".join(_ddl_blocks())
    assert "to_regclass('public.earnings_provider_pairs')" in blocks
    assert "DROP TABLE earnings_provider_pairs" in blocks
    assert "SELECT symbol, market, fiscal_year, fiscal_quarter, source, 'EPS_GAAP'" in blocks
