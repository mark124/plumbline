"""End-to-end tests of the command, with the catalog stubbed out.

These cover the contract CI depends on: what gets written, and what the exit
code is. An exit code regression here would silently turn a merge gate into a
rubber stamp.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from plumbline import cli as cli_module

from .fakes import FakeCatalog

ORDERS = {
    "order_id": "NUMBER",
    "customer_id": "NUMBER",
    "order_total": "NUMBER",
}


@pytest.fixture
def patched(monkeypatch):
    catalog = FakeCatalog(tables={"analytics.public.orders": ORDERS})
    monkeypatch.setattr(cli_module, "_connect", lambda server, token: object())
    monkeypatch.setattr(cli_module, "DataHubCatalog", lambda *a, **k: catalog)
    return catalog


def _run(args):
    return CliRunner().invoke(cli_module.main, args)


def _write(tmp_path, sql, name="model.sql"):
    p = tmp_path / name
    p.write_text(sql, encoding="utf-8")
    return str(p)


BASE = ["--database", "analytics", "--schema", "public"]


def test_clean_file_exits_zero(patched, tmp_path):
    path = _write(tmp_path, "SELECT order_id FROM analytics.public.orders")
    res = _run(["check", path, *BASE])
    assert res.exit_code == 0, res.output
    assert "nothing to report" in res.output


def test_phantom_column_exits_nonzero(patched, tmp_path):
    path = _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders")
    res = _run(["check", path, *BASE])
    assert res.exit_code == 1
    assert "order_ttl" in res.output


def test_unknown_table_does_not_fail_the_build(patched, tmp_path):
    """An uningested table must never break someone's pipeline."""
    path = _write(tmp_path, "SELECT a FROM analytics.public.shipments")
    res = _run(["check", path, *BASE])
    assert res.exit_code == 0, res.output


def test_fail_on_warn_escalates(patched, tmp_path):
    patched.deprecated.add("analytics.public.orders")
    path = _write(tmp_path, "SELECT order_id FROM analytics.public.orders")
    assert _run(["check", path, *BASE]).exit_code == 0
    assert _run(["check", path, *BASE, "--fail-on", "warn"]).exit_code == 1


def test_fail_on_never_always_exits_zero(patched, tmp_path):
    path = _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders")
    res = _run(["check", path, *BASE, "--fail-on", "never"])
    assert res.exit_code == 0


def test_json_output_is_valid_and_structured(patched, tmp_path):
    path = _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders")
    out = tmp_path / "findings.json"
    res = _run(["check", path, *BASE, "--format", "json", "--out", str(out)])
    assert res.exit_code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"]["error"] == 1
    assert data["findings"][0]["check"] == "phantom_column"
    assert data["findings"][0]["suggestion"] == "order_total"


def test_markdown_output_written(patched, tmp_path):
    path = _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders")
    out = tmp_path / "report.md"
    res = _run(["check", path, *BASE, "--format", "markdown", "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert text.startswith("## Plumbline")
    assert "order_ttl" in text


def test_directory_expansion(patched, tmp_path):
    _write(tmp_path, "SELECT order_id FROM analytics.public.orders", "a.sql")
    _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders", "b.sql")
    res = _run(["check", str(tmp_path), *BASE])
    assert res.exit_code == 1
    assert "2 file(s)" in res.output


def test_selecting_a_single_check(patched, tmp_path):
    path = _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders")
    res = _run(["check", path, *BASE, "--check", "blast_radius"])
    assert res.exit_code == 0, res.output


def test_missing_file_is_a_clean_error(patched, tmp_path):
    res = _run(["check", str(tmp_path / "nope.sql"), *BASE])
    assert res.exit_code != 0
    assert "Not found" in res.output


# --- which files actually get opened --------------------------------------
#
# Every failure here is silent: a file that is never read is never reported
# on, and the run still ends with "clean". These were found by handing the
# expander the paths a real invocation produces rather than tidy ones.


def test_uppercase_extension_is_not_skipped(patched, tmp_path):
    """glob's match is case-sensitive on Linux, so `*.sql` misses QUERY.SQL.

    On a developer's Windows machine this passes and in CI it silently
    checks nothing, which is the worst possible split.
    """
    _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders", "QUERY.SQL")
    res = _run(["check", str(tmp_path), *BASE])
    assert res.exit_code == 1, res.output
    assert "order_ttl" in res.output


def test_filename_containing_glob_characters_is_read(patched, tmp_path):
    """`report[1].sql` is a real filename and a pattern matching nothing."""
    path = _write(
        tmp_path, "SELECT order_ttl FROM analytics.public.orders", "report[1].sql"
    )
    res = _run(["check", path, *BASE])
    assert res.exit_code == 1, res.output
    assert "order_ttl" in res.output


def test_the_same_file_twice_is_checked_once(patched, tmp_path):
    """A changed-files list can name a path the directory arg also covers."""
    path = _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders")
    res = _run(["check", path, path, *BASE])
    assert "1 file(s)" in res.output
    assert res.output.count("order_ttl` does not exist") == 1


def test_a_directory_and_a_file_inside_it_do_not_double_count(patched, tmp_path):
    _write(tmp_path, "SELECT order_ttl FROM analytics.public.orders", "a.sql")
    res = _run(["check", str(tmp_path), str(tmp_path / "a.sql"), *BASE])
    assert "1 file(s)" in res.output
