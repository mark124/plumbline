"""Check that every shot in DEMO.md will actually work, before recording.

Discovering halfway through a take that the catalog is down, or that the shot
needing an API key cannot run, costs a session. This runs each shot's real
command and reports what is ready and what is missing, with the remedy.

    python demo/preflight.py

Exits 0 when every shot can be recorded, 1 otherwise.
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SERVER = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8081")
BASE = ["--platform", "snowflake", "--platform-instance", "b2fd91"]

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    GREEN = RED = DIM = OFF = ""

results: list[tuple[bool, str, str, str]] = []


def record(ok: bool, shot: str, detail: str, remedy: str = "") -> None:
    results.append((ok, shot, detail, remedy))


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "plumbline.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "DATAHUB_GMS_URL": SERVER},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def headline(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Plumbline:"):
            return line.strip()
    return out.strip().splitlines()[0] if out.strip() else "(no output)"


# --- the catalog itself ---------------------------------------------------

code, out = run(["check", "examples/order_details_rebuild.sql", *BASE])
reachable = "not reachable" not in out and "Could not connect" not in out
record(
    reachable,
    "catalog",
    f"DataHub at {SERVER}" + ("" if reachable else " is not answering"),
    f"Start Docker Desktop, then: datahub docker quickstart. Note that the "
    f"quickstart reports failure on Windows even when it succeeds, so check "
    f"container health rather than its exit code.",
)

if reachable:
    # --- shot 2: three findings on one file ------------------------------
    ok = "1 error" in out and "1 info" in out
    record(ok, "shot 2", headline(out),
           "Expected 1 error, 1 warning, 1 info. Re-run demo/seed_demo_catalog.py.")

    # --- shot 3: the honesty rule ----------------------------------------
    code, out3 = run(["check", "examples/uningested_table.sql", *BASE])
    ok = code == 0 and "1 unknown" in out3
    record(ok, "shot 3", f"{headline(out3)}  exit={code}",
           "Expected exactly 1 unknown and exit 0. This is the shot that "
           "argues the tool will not waste your time; do not record without it.")

    # --- shot 6: write-back ----------------------------------------------
    code, out6 = run(["check", "models/", *BASE, "--publish"])
    published = next(
        (l for l in out6.splitlines() if l.startswith("Published")), ""
    )
    record(bool(published), "shot 6", published or "nothing published",
           "Needs a live catalog it can write to.")

# --- shot 4: the agent ----------------------------------------------------

has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
try:
    import anthropic  # noqa: F401
    import mcp  # noqa: F401
    import mcp_server_datahub  # noqa: F401

    deps = True
except ImportError:
    deps = False

record(
    has_key and deps,
    "shot 4",
    ("agent ready" if has_key and deps else
     ("ANTHROPIC_API_KEY is not set" if deps else "fix dependencies missing")),
    "set ANTHROPIC_API_KEY, or record shots 1-3 and 5-7 and cut shot 4. "
    "Losing it costs the strongest evidence that this is an agent and not a "
    "linter, so it is worth setting the key.",
)

# --- shot 7: the pull request --------------------------------------------

try:
    proc = subprocess.run(
        ["gh", "pr", "view", "1", "--json", "state,statusCheckRollup",
         "--jq", '.state + " " + ([.statusCheckRollup[]|.name+"="+.conclusion]|join(" "))'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    line = (proc.stdout or "").strip()
    ok = "OPEN" in line and "gate=FAILURE" in line and "test (3.12)=SUCCESS" in line
    record(ok, "shot 7", line or (proc.stderr or "").strip()[:80],
           "PR #1 must be OPEN with the gate red and the unit tests green. "
           "That contrast is the shot.")
except Exception as exc:  # noqa: BLE001
    record(False, "shot 7", f"could not read PR ({type(exc).__name__})",
           "gh auth login")

# --- report ---------------------------------------------------------------

print()
for ok, shot, detail, _ in results:
    mark = f"{GREEN}ready{OFF}" if ok else f"{RED} gap {OFF}"
    print(f"  [{mark}] {shot:9} {detail}")

blocked = [r for r in results if not r[0]]
print()
if not blocked:
    print(f"{GREEN}All shots can be recorded.{OFF}")
    print(f"{DIM}Record shots 6 and 7 first: they are the two that need the "
          f"live catalog, and it is the part most likely to go away.{OFF}")
    sys.exit(0)

print(f"{RED}{len(blocked)} shot(s) cannot be recorded yet:{OFF}")
for _, shot, _, remedy in blocked:
    print(f"  {shot}: {remedy}")
sys.exit(1)
