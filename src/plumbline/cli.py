"""Plumbline command line interface."""

from __future__ import annotations

import glob
import logging
import os
import sys
from typing import List, Optional, Sequence

import click

from .catalog import CatalogUnavailable, DataHubCatalog
from .checks import ALL_CHECKS, run_all
from .findings import Report
from .parse import parse_sql, split_statements
from .report import render_html, render_json, render_markdown, render_text


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


def _run_fix_agent(
    report,
    statements,
    catalog,
    *,
    server_url,
    token,
    model,
    dialect,
    database,
    schema_,
) -> None:
    """Ask the agent for repairs, then report what survived verification."""
    import asyncio

    from .agent import FixAgent, apply_fixes

    if not server_url:
        raise click.ClickException(
            "--fix needs an explicit --server (or DATAHUB_GMS_URL) so the MCP "
            "server can reach the same catalog."
        )

    kwargs = {"gms_url": server_url, "gms_token": token}
    if model:
        kwargs["model"] = model
    agent = FixAgent(**kwargs)

    all_fixes = []
    for statement, produced in statements:
        blocking = [f for f in produced if f.severity.blocking]
        if not blocking:
            continue
        try:
            fixes = asyncio.run(
                agent.propose_all(
                    blocking,
                    statement,
                    catalog,
                    dialect=dialect,
                    default_db=database,
                    default_schema=schema_,
                    # Everything this statement already produced, so a rewrite
                    # that adds a problem the original did not have is caught.
                    baseline=produced,
                )
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(f"Fix agent unavailable: {exc}", err=True)
            return
        all_fixes.extend(fixes)

    apply_fixes(report, all_fixes)

    accepted = [f for f in all_fixes if f.accepted]
    click.echo(
        f"Fix agent: {len(accepted)} of {len(all_fixes)} proposals passed "
        "re-verification."
    )
    for f in all_fixes:
        if not f.accepted:
            click.echo(f"  - {f.finding.summary}: {f.reason}")


@click.group()
@click.version_option(package_name="plumbline")
def main() -> None:
    """Check AI-written data code against the DataHub catalog."""
    # The DataHub SDK logs full urllib3 tracebacks when it retries a request.
    # Inside a CI log that buries the one line the reader needs. Plumbline
    # reports connectivity failures itself, with a message that says what to
    # do, so the SDK's copy is noise. PLUMBLINE_DEBUG=1 puts it back.
    if not os.environ.get("PLUMBLINE_DEBUG"):
        logging.getLogger("datahub").setLevel(logging.CRITICAL)
        logging.getLogger("urllib3").setLevel(logging.CRITICAL)


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
    type=click.Choice(["text", "markdown", "json", "html"]),
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
@click.option(
    "--fix",
    is_flag=True,
    help=(
        "Ask the agent to propose repairs for blocking findings, via the "
        "DataHub MCP server. Every proposal is re-checked before it is shown; "
        "unverified repairs are discarded. Needs ANTHROPIC_API_KEY."
    ),
)
@click.option(
    "--model",
    default=None,
    help="Model for --fix. Defaults to the agent's own default.",
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
    fix,
    model,
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
    # Keep each statement alongside the findings it produced, so the fix agent
    # can be handed the exact text a finding refers to.
    statements: List = []
    for path in files:
        # utf-8-sig strips a byte order mark if one is present. Editors and
        # shells on Windows write them by default, and a leading BOM makes the
        # whole statement unparseable, so the file would be reported as
        # unchecked for a reason that has nothing to do with its SQL.
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
        if not text.strip():
            continue
        report.files_checked += 1
        for statement, first_line in split_statements(text, dialect=dialect):
            before = len(report.findings)
            try:
                parsed = parse_sql(
                    statement,
                    catalog,
                    dialect=dialect,
                    default_db=database,
                    default_schema=schema_,
                    file=path,
                    line_offset=first_line - 1,
                )
                run_all(parsed, catalog, report, enabled=enabled)
            except CatalogUnavailable as exc:
                # Deliberately not a partial report. Findings gathered before
                # the catalog went away are fine, but the ones after it would
                # be fabricated, and a half-checked file presented as a
                # checked one is worse than no answer.
                raise click.ClickException(
                    f"{exc}\n\nNo report was produced. Re-run when DataHub is "
                    "reachable."
                ) from exc
            produced = report.findings[before:]
            if produced:
                statements.append((statement, produced))

    if fix and report.blocking_count:
        _run_fix_agent(
            report,
            statements,
            catalog,
            server_url=getattr(graph, "_gms_server", None) or server,
            token=token,
            model=model,
            dialect=dialect,
            database=database,
            schema_=schema_,
        )

    rendered = {
        "text": render_text,
        "markdown": render_markdown,
        "json": render_json,
        "html": render_html,
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
