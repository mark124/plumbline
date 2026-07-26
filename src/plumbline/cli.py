"""Plumbline command line interface."""

from __future__ import annotations

import glob
import os
import sys
from typing import List, Optional, Sequence

import click

from .catalog import DataHubCatalog
from .checks import ALL_CHECKS, run_all
from .findings import Report
from .parse import parse_sql, split_statements
from .report import render_json, render_markdown, render_text


def _connect(server: Optional[str], token: Optional[str]):
    """Open a DataHub graph connection.

    Falls back to ~/.datahubenv, which is what `datahub init` writes, so the
    common case needs no flags at all.
    """
    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

    if server:
        return DataHubGraph(DatahubClientConfig(server=server, token=token))

    from datahub.cli.config_utils import load_client_config

    config = load_client_config()
    if token:
        config.token = token
    return DataHubGraph(config)


def _expand(paths: Sequence[str]) -> List[str]:
    """Expand file paths and directories into a list of .sql files."""
    out: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(sorted(glob.glob(os.path.join(p, "**", "*.sql"), recursive=True)))
        elif any(ch in p for ch in "*?["):
            out.extend(sorted(glob.glob(p, recursive=True)))
        else:
            out.append(p)
    return out


@click.group()
@click.version_option(package_name="plumbline")
def main() -> None:
    """Check AI-written data code against the DataHub catalog."""


@main.command()
@click.argument("paths", nargs=-1, required=True)
@click.option("--server", envvar="DATAHUB_GMS_URL", help="DataHub GMS URL.")
@click.option("--token", envvar="DATAHUB_GMS_TOKEN", help="DataHub access token.")
@click.option("--platform", default="snowflake", show_default=True, help="Data platform to resolve against.")
@click.option("--env", "env_", default="PROD", show_default=True, help="DataHub environment (fabric).")
@click.option("--platform-instance", default=None, help="Platform instance, if your catalog uses one.")
@click.option("--database", default=None, help="Default database for unqualified table names.")
@click.option("--schema", "schema_", default=None, help="Default schema for unqualified table names.")
@click.option("--dialect", default="snowflake", show_default=True, help="SQL dialect to parse with.")
@click.option(
    "--format",
    "format_",
    type=click.Choice(["text", "markdown", "json"]),
    default="text",
    show_default=True,
)
@click.option("--out", type=click.Path(dir_okay=False), help="Write the report to a file.")
@click.option(
    "--fail-on",
    type=click.Choice(["error", "warn", "never"]),
    default="error",
    show_default=True,
    help="Which severity should make the command exit nonzero.",
)
@click.option(
    "--check",
    "checks_",
    multiple=True,
    type=click.Choice(list(ALL_CHECKS)),
    help="Run only these checks. Repeatable. Defaults to all.",
)
def check(
    paths,
    server,
    token,
    platform,
    env_,
    platform_instance,
    database,
    schema_,
    dialect,
    format_,
    out,
    fail_on,
    checks_,
):
    """Check SQL files against the catalog."""
    files = _expand(paths)
    if not files:
        raise click.ClickException("No SQL files matched.")

    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise click.ClickException(f"Not found: {', '.join(missing)}")

    try:
        graph = _connect(server, token)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"Could not connect to DataHub: {exc}\n"
            "Set --server (or DATAHUB_GMS_URL), or run `datahub init` first."
        ) from exc

    catalog = DataHubCatalog(
        graph, platform=platform, env=env_, platform_instance=platform_instance
    )
    enabled = list(checks_) if checks_ else list(ALL_CHECKS)

    report = Report()
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if not text.strip():
            continue
        report.files_checked += 1
        for statement in split_statements(text, dialect=dialect):
            parsed = parse_sql(
                statement,
                catalog,
                dialect=dialect,
                default_db=database,
                default_schema=schema_,
                file=path,
            )
            run_all(parsed, catalog, report, enabled=enabled)

    rendered = {
        "text": render_text,
        "markdown": render_markdown,
        "json": render_json,
    }[format_](report)

    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        click.echo(f"Report written to {out}")
        if format_ != "json":
            click.echo(render_text(report))
    else:
        click.echo(rendered)

    if fail_on == "never":
        sys.exit(0)
    if fail_on == "warn":
        counts = report.counts
        sys.exit(1 if counts["error"] or counts["warn"] else 0)
    sys.exit(report.exit_code)


if __name__ == "__main__":
    main()
