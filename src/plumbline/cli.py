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
    """Expand file paths, directories and globs into a list of .sql files.

    Getting this wrong is a quiet failure: a file that is never opened is
    never reported on, and the run still ends with "clean". So each rule here
    errs toward reading a file rather than skipping it.
    """
    out: List[str] = []
    seen = set()

    def add(path: str) -> None:
        # The same file arrives twice more often than you would think: a
        # changed-files list plus the directory that contains it, or one path
        # spelled two ways. Checking it twice doubles every finding in it and
        # inflates the file count in the summary.
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    for p in paths:
        if os.path.isdir(p):
            # os.walk rather than glob. glob's pattern match is case-sensitive
            # on Linux, so `*.sql` silently skips a file named QUERY.SQL and
            # the CI job reports success on a file it never opened.
            for dirpath, dirs, names in os.walk(p):
                dirs.sort()
                for name in sorted(names):
                    if name.lower().endswith(".sql"):
                        add(os.path.join(dirpath, name))
        elif os.path.isfile(p):
            # Tested before the glob branch. `report[1].sql` is a real
            # filename and also a valid pattern that matches nothing, and
            # dropping a named file without a word is the worst outcome here.
            add(p)
        elif any(ch in p for ch in "*?["):
            for match in sorted(glob.glob(p, recursive=True)):
                add(match)
        else:
            # Does not exist. Kept so the caller can say so by name.
            add(p)
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
@click.option(
    "--demo",
    is_flag=True,
    help=(
        "Check against the bundled snapshot of the public showcase catalog "
        "instead of a live DataHub. Same checker, same checks, same report; "
        "only the source of the facts changes. Use it to see the tool work "
        "before you have a catalog of your own to point it at."
    ),
)
@click.option(
    "--snapshot",
    type=click.Path(dir_okay=False, exists=True),
    default=None,
    help="Check against a specific snapshot file. Implies --demo.",
)
@click.option(
    "--publish",
    is_flag=True,
    help=(
        "Record this run's verdict back to DataHub as an assertion on each "
        "dataset, so the conclusion joins the graph instead of scrolling past "
        "in a CI log. Written by the deterministic layer only; the agent "
        "cannot reach it. Off by default because a checker should not edit a "
        "shared catalog uninvited."
    ),
)
@click.option(
    "--run-url",
    default=None,
    envvar="PLUMBLINE_RUN_URL",
    help="Link recorded on published assertions, e.g. the CI run that produced them.",
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
    demo,
    snapshot,
    publish,
    run_url,
):
    """Check SQL files against the catalog."""
    files = _expand(paths)
    if not files:
        raise click.ClickException("No SQL files matched.")

    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise click.ClickException(f"Not found: {', '.join(missing)}")

    graph = None
    if demo or snapshot:
        from .snapshot import SnapshotCatalog

        try:
            catalog = SnapshotCatalog.load(snapshot)
        except FileNotFoundError as exc:
            raise click.ClickException(
                f"No snapshot at {snapshot or 'the bundled path'}. "
                "Build one with `plumbline snapshot --server <url>`."
            ) from exc
        # Said out loud, every run. The findings below are real, and the
        # catalog behind them is frozen and is not the reader's warehouse.
        click.echo(f"Reading a {catalog.describe()}.", err=True)
        click.echo(
            "This is not a live catalog. Drop --demo and pass --server to "
            "check against your own.\n",
            err=True,
        )
        if publish:
            raise click.ClickException(
                "--publish needs a live catalog to write to; --demo is read-only."
            )
    else:
        try:
            graph = _connect(server, token)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(
                f"Could not connect to DataHub: {exc}\n"
                "Set --server (or DATAHUB_GMS_URL), or run `datahub init` first.\n"
                "To see the tool work without a catalog, use --demo."
            ) from exc

        catalog = DataHubCatalog(
            graph, platform=platform, env=env_, platform_instance=platform_instance
        )
    enabled = list(checks_) if checks_ else list(ALL_CHECKS)

    report = Report()
    # Keep each statement alongside the findings it produced, so the fix agent
    # can be handed the exact text a finding refers to.
    statements: List = []
    # Datasets this run actually resolved. Needed for --publish: an assertion
    # that only ever appears on failure says nothing about the datasets that
    # were fine, and "no news" is not the same as "checked and clean".
    resolved_urns: List[str] = []
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
                for ref in parsed.tables:
                    if ref.exists and ref.urn not in resolved_urns:
                        resolved_urns.append(ref.urn)
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

    if publish:
        from .publish import publish as publish_report

        written = publish_report(
            report, graph, checked_urns=resolved_urns, source_url=run_url
        )
        click.echo(
            f"Published {written.written} assertion(s) to DataHub: "
            f"{written.passed} passing, {written.failed} failing."
        )
        for problem in written.errors:
            # Publishing is a side effect, never the point. A catalog that
            # will not take the write must not turn a correct report into a
            # failed run, so this is said out loud and the report still stands.
            click.echo(f"  could not publish: {problem}", err=True)

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


@main.command()
@click.option("--server", envvar="DATAHUB_GMS_URL", required=True, help="DataHub GMS URL.")
@click.option("--token", envvar="DATAHUB_GMS_TOKEN", help="DataHub access token.")
@click.option("--platform", default="snowflake", show_default=True)
@click.option("--env", "env_", default="PROD", show_default=True)
@click.option("--platform-instance", default=None)
@click.option("--out", type=click.Path(dir_okay=False), default=None,
              help="Where to write the snapshot. Defaults to the bundled path.")
def snapshot(server, token, platform, env_, platform_instance, out):
    """Freeze a live catalog to a file, so `check --demo` has something to read.

    Captures only what the checks consume: schemas, column tags, deprecation,
    lineage and query text. Not a general metadata export.
    """
    import json

    from .snapshot import DEFAULT_SNAPSHOT, export

    graph = _connect(server, token)
    data = export(graph, platform, env_, platform_instance)
    target = out or DEFAULT_SNAPSHOT
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    columns = sum(len(t["columns"]) for t in data["tables"].values())
    click.echo(
        f"Wrote {target}: {len(data['tables'])} datasets, {columns} columns, "
        f"{len(data['queries'])} with query history."
    )


if __name__ == "__main__":
    main()
