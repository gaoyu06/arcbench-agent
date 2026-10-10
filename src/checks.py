"""Deterministic verification of the generated app: install, build, cold start, static rules, e2e tests."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import sandbox
from tools import NPM_CACHE, SECRET_ENV, clip, kill_group, shell

# Browser dialogs are dismissed automatically by the test browser (rule 10).
STATIC_RULES = [
    (re.compile(r"\b(?:window\.)?(?:alert|confirm|prompt)\s*\("), "alert/confirm/prompt: use an in-page dialog"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def install(root: Path) -> str:
    """npm install where node_modules is missing or older than package.json."""
    out = []
    for part in ("backend", "frontend"):
        pkg, modules = root / part / "package.json", root / part / "node_modules"
        if modules.is_dir() and modules.stat().st_mtime >= pkg.stat().st_mtime:
            continue
        result = shell("npm install --no-audit --no-fund --loglevel=error", root / part, 600, workspace=root)
        if result.rstrip().endswith("[exit 0]"):
            modules.touch()
        else:
            out.append(f"npm install failed in {part}:\n{result}")
    return "\n".join(out)


def build(root: Path) -> str:
    """Empty string when the frontend builds, otherwise the relevant error output."""
    problem = install(root)
    if problem:
        return problem
    result = shell("npm run build", root / "frontend", 300, workspace=root)
    return "" if result.rstrip().endswith("[exit 0]") else f"frontend build failed:\n{result}"


# The database the earlier stage's app left after its own tests: the evaluator starts a later stage's app on the
# earlier stage's data, so verification must exercise the new code on a copy of it.
STAGE_DB = ".agent/stage-db"


class Server:
    """Backend started as in production (`npm start`), on a fresh database or a copy of the files in `data`."""

    def __init__(self, root: Path, data: Path | None = None):
        self.root, self.port = root, free_port()
        self.dir = tempfile.mkdtemp(prefix="arc-db-")
        for item in data.iterdir() if data else []:
            shutil.copy2(item, self.dir)
        self.log = Path(self.dir) / "server.log"
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env.update(
            PORT=str(self.port),
            ARC_DB_FILE=f"{self.dir}/app.db",
            DATABASE_FILE=f"{self.dir}/app.db",
            TMPDIR=self.dir,
            npm_config_cache=str(NPM_CACHE),
            npm_config_userconfig="/dev/null",
        )
        argv = sandbox.command(["npm", "start"], [root, Path(self.dir), NPM_CACHE])
        self.proc = subprocess.Popen(
            argv,
            cwd=root / "backend",
            env=env,
            start_new_session=True,
            stdout=self.log.open("w"),
            stderr=subprocess.STDOUT,
        )
        self.url = f"http://127.0.0.1:{self.port}"

    def wait(self, seconds: int = 40) -> str:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return f"backend exited with code {self.proc.returncode}:\n{self.output()}"
            try:
                with urllib.request.urlopen(self.url + "/", timeout=3) as r:
                    if r.status < 500:
                        return ""
            except Exception:
                time.sleep(0.5)
        return f"backend did not answer on / within {seconds}s:\n{self.output()}"

    def output(self) -> str:
        return clip(self.log.read_text(errors="replace"), 4000) if self.log.exists() else ""

    def stop(self) -> None:
        kill_group(self.proc.pid)


EARLIER_DB = "backend/database.db"  # the app's default database file, delivered with an earlier stage's workspace


def keep_stage_db(root: Path) -> str:
    """Before a later stage changes the code: keep the earlier stage's database in STAGE_DB. That is the database
    file delivered with the workspace when there is one, else the one the earlier app's own tests leave on a fresh
    database. Returns a problem, empty when the database was kept."""
    delivered = sorted((root / EARLIER_DB).parent.glob("database.db*"))
    if (root / EARLIER_DB).is_file():
        (root / STAGE_DB).mkdir(parents=True, exist_ok=True)
        for item in delivered:  # Server opens STAGE_DB/app.db, with its -wal/-journal files beside it
            shutil.copy2(item, root / STAGE_DB / item.name.replace("database.db", "app.db", 1))
        return ""
    problem = build(root)
    if problem:
        return problem
    server = Server(root)
    try:
        problem = server.wait()
        if not problem:
            e2e(root, server.url)
    finally:
        server.stop()
    files = list(Path(server.dir).glob("app.db*"))
    if problem or not files:
        return problem or "the earlier app wrote no database file at ARC_DB_FILE"
    (root / STAGE_DB).mkdir(parents=True, exist_ok=True)
    for item in files:
        shutil.copy2(item, root / STAGE_DB)
    return ""


def static(root: Path) -> list[str]:
    """Violations of the component rules."""
    found = []
    for path in sorted((root / "frontend" / "src").rglob("*.[jt]s*")):
        for no, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for pattern, why in STATIC_RULES:
                if pattern.search(line):
                    found.append(f"{path.relative_to(root)}:{no}: {why}")
    return found


# Against a local server a correct action finishes in milliseconds, so a stuck locator fails after 3 s instead
# of using up the whole 15 s test timeout. Applied on top of the project's own config.
FAST_OVERRIDE = """const base = require('./{base}');
const config = base.default || base;
module.exports = {{ ...config, expect: {{ ...(config.expect || {{}}), timeout: 3000 }},
  use: {{ ...(config.use || {{}}), actionTimeout: 3000, navigationTimeout: 5000, trace: 'off' }} }};
"""


SNAPSHOTS = 2  # failures that get the page snapshot Playwright records at the failing step


def page_snapshot(result: dict) -> str:
    """The accessibility snapshot of the page at a failure (Playwright's error-context attachment), indented so
    it stays inside its failure entry; empty when there is none."""
    path = next((a.get("path") for a in result.get("attachments") or [] if a.get("name") == "error-context"), None)
    try:
        text = Path(path).read_text(errors="replace") if path else ""
    except OSError:
        return ""
    lines = [
        re.sub(r" \[ref=e\d+\]", "", line)
        for line in text.splitlines()
        if line.strip() and not line.startswith(("#", "```"))
    ]
    return "\n".join(f"    {line}" for line in clip("\n".join(lines), 1200).splitlines())


def e2e(root: Path, url: str, pattern: str = "", config: str = "", workers: int = 4) -> tuple[int, int, str]:
    """Run Playwright tests (backend/test-e2e unless `config` names another config).

    Returns (passed, total, failures)."""
    tests = root / "backend" / "test-e2e"
    if not config and (not tests.is_dir() or not any(tests.glob("*.spec.*"))):
        return 0, 0, "no e2e tests in backend/test-e2e"
    report = Path(tempfile.mkdtemp(prefix="arc-pw-")) / "report.json"
    base = config or "playwright.config.js"
    fast = root / "backend" / f".check-{report.parent.name}.config.cjs"  # unique: runs may overlap
    if (root / "backend" / base).is_file():
        fast.write_text(FAST_OVERRIDE.format(base=base))
        cfg = f" --config={fast.name}"
    else:
        cfg = f" --config={config}" if config else ""
    cmd = (
        f"npx playwright test {pattern}{cfg} --reporter=json --workers={workers} --timeout=15000 --retries=0"
        f" > {report} 2>/dev/null"
    )
    try:
        shell(
            cmd,
            root / "backend",
            900,
            writable=[report.parent],
            workspace=root,
            env={"PLAYWRIGHT_BASE_URL": url, "ARC_WEB_BASE_URL": url, "E2E_BASE_URL": url, "BASE_URL": url, "CI": "1"},
        )
    finally:
        fast.unlink(missing_ok=True)
    try:
        data = json.loads(report.read_text())
    except (ValueError, OSError):
        return (
            0,
            0,
            "playwright produced no report (syntax error in a spec?):\n"
            + shell(f"npx playwright test {pattern} --list", root / "backend", 120, env={"CI": "1"}, workspace=root),
        )
    passed, total, failures = 0, 0, list[str]()

    def walk(suite: dict) -> None:
        nonlocal passed, total
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                total += 1
                if test.get("status") == "expected":
                    passed += 1
                else:
                    result = (test.get("results") or [{}])[0]
                    # errors[] also holds the pending action ("waiting for getByLabel(...)") behind a timeout
                    messages = [e.get("message", "") for e in result.get("errors") or []]
                    messages = messages or [(result.get("error") or {}).get("message", "")]
                    error = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(dict.fromkeys(messages)))
                    detail = clip(error, 700)
                    blockers = dict.fromkeys(
                        line.strip() for line in error.splitlines() if "intercepts pointer events" in line
                    )
                    hidden = [line for line in blockers if line not in detail]
                    if hidden:
                        detail += "\n  Click interception:\n" + "\n".join(f"    {line}" for line in hidden)
                    snapshot = page_snapshot(result) if len(failures) < SNAPSHOTS else ""
                    failures.append(
                        f"- {spec.get('file')} :: {spec.get('title')}\n  {detail}"
                        + (f"\n  Page at the failure:\n{snapshot}" if snapshot else "")
                    )
        for child in suite.get("suites", []):
            walk(child)

    for suite in data.get("suites", []):
        walk(suite)
    for error in data.get("errors", []):
        failures.append(f"- load error: {clip(re.sub(chr(27) + r'\[[0-9;]*m', '', error.get('message', '')), 700)}")
    return passed, total, "\n".join(failures)


RERUN_MAX_FAILURES = 10  # a rerun stops early when failures are real, not load


def failure_names(failures: str) -> list[str]:
    """Names ("file :: title") of the tests that failed in `e2e` output; those listed as FLAKY passed on a rerun."""
    return re.findall(r"^- (.+ :: .+)$", failures.split("FLAKY (", 1)[0], re.M)


def confirm(root: Path, url: str, failures: str) -> tuple[str, int]:
    """Rerun the failed tests of a parallel run one at a time. Returns the failures that remain, with the
    tests that passed on the rerun listed as FLAKY, and the number of those tests."""
    names = failure_names(failures)
    if not names:
        return failures, 0
    grep = "|".join(re.escape(name.split(" :: ", 1)[1]) for name in names)
    _, total, again = e2e(root, url, f"--grep {shlex.quote(grep)} --max-failures={RERUN_MAX_FAILURES}", workers=1)
    if not total:
        return failures, 0
    still = set(failure_names(again))
    recovered = [name for name in names if name not in still]
    if not recovered:
        return failures, 0
    kept = [
        entry
        for entry in re.split(r"\n(?=- )", failures.strip())
        if not any(entry.startswith(f"- {name}\n") or entry == f"- {name}" for name in recovered)
    ]
    flaky = (
        "FLAKY (failed in the parallel run, passed when rerun one at a time; tests probably share state "
        "with parallel tests):\n" + "\n".join(f"- {name}" for name in recovered)
    )
    return "\n".join([*kept, flaky]), len(recovered)


def failed_tests(report: str) -> set[str]:
    """Names ("file :: title") listed in a check report's FAILED TESTS section."""
    section = (
        report.split("FAILED TESTS:\n", 1)[1].split("\nE2E FAILURES:", 1)[0] if "FAILED TESTS:\n" in report else ""
    )
    return {line[2:] for line in section.splitlines() if line.startswith("- ")}


def spec_parts(pattern: str) -> list[str]:
    """The alternatives of a check pattern: 'a.spec|b.spec' (or space separated) names both files."""
    return [part for part in re.split(r"[|\s]+", pattern) if part]


def spec_filter(pattern: str) -> str:
    """Playwright file filters for a check pattern, one quoted argument per alternative. Unquoted, the shell
    read the | as a pipe and no test ran."""
    return " ".join(shlex.quote(part) for part in spec_parts(pattern))


def unmatched(root: Path, pattern: str) -> str:
    """An explanation when no e2e spec file matches the pattern (Playwright reads each part as a regular
    expression on the file path), so no build is spent on a run without tests; empty otherwise."""
    specs = sorted(str(p.relative_to(root / "backend")) for p in (root / "backend" / "test-e2e").glob("*.spec.*"))
    parts = spec_parts(pattern)

    def matches(part: str, spec: str) -> bool:
        try:
            return re.search(part, spec) is not None
        except re.error:
            return part in spec

    if not parts or any(matches(part, spec) for part in parts for spec in specs):
        return ""
    return f"no e2e spec file matches the pattern {pattern!r}; spec files: {', '.join(specs) or 'none'}"


def missing_seeds(db: Path, values: list[str]) -> list[str]:
    """Values not found verbatim in database text; these may be derived or composite scenario phrases."""
    if not values or not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        texts: list[str] = []
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for row in con.execute(f'SELECT * FROM "{table}"'):
                texts.extend(str(v) for v in row if isinstance(v, (str, bytes)))
    except sqlite3.Error:
        return []
    finally:
        con.close()
    blob = "\n".join(texts).lower()
    return [v for v in values if v.lower() not in blob]


SEED_HEADER = "SEED DATA SCAN (heuristic)"


def check(root: Path, pattern: str = "", seeds: list[str] | None = None) -> tuple[bool, str]:
    """Full verification used by the `check` tool and the final gate. `seeds` are values the requirements'
    scenario preconditions name; the report lists those a fresh start does not put in the database.
    The same scenarios also run on a copy of the earlier stage's data when it exists."""
    problem = unmatched(root, pattern)
    if problem:
        return False, problem
    pattern = spec_filter(pattern)
    problem = build(root)
    if problem:
        return False, problem
    datasets: list[tuple[str, Path | None]] = [("fresh", None)]
    if (root / STAGE_DB).is_dir():
        datasets.append(("earlier-stage", root / STAGE_DB))
    summaries: list[str] = []
    details: list[str] = []
    outputs: list[str] = []
    names: list[str] = []
    browser: list[str] = []
    missing = []
    ok = True
    for label, data in datasets:
        server = Server(root, data) if data else Server(root)
        try:
            problem = server.wait()
            if problem:
                return False, f"the backend does not start on the {label} database:\n{problem}"
            if data is None and seeds:
                missing = missing_seeds(Path(server.dir) / "app.db", seeds)
            passed, total, failures = e2e(root, server.url, pattern)
            failures, recovered = confirm(root, server.url, failures)
            passed += recovered
            output = server.output()
        finally:
            server.stop()
        summaries.append(f"{label}-database start ok; e2e {passed}/{total} passed")
        ok = ok and total > 0 and passed == total
        if failures:
            details.append(f"{label} database:\n{failures}")
            names.extend(failure_names(failures))
            if output.strip():
                outputs.append(f"{label} database:\n{output.strip()}")
        browser.extend(f"{label} database: {line}" for line in re.findall(r"^\[browser error\] (.+)$", output, re.M))
    violations = static(root)
    lines = ["build ok; " + "; ".join(summaries)]
    if missing:
        lines.append(
            f"{SEED_HEADER}: these phrases were not found verbatim in database text after a fresh "
            "start. They may be composite expressions, derived values or expected absences. Use the "
            "published scenarios to decide whether a record is missing before changing seed data:\n"
            + "\n".join(f"- {v}" for v in missing[:80])
        )
    if details:
        if outputs:
            lines.append("BACKEND OUTPUT (captured during the failed tests):\n" + "\n".join(outputs))
        names = list(dict.fromkeys(names))
        if names:  # complete list; the details below may be clipped
            lines.append("FAILED TESTS:\n" + "\n".join(f"- {name}" for name in names[:300]))
        lines.append("E2E FAILURES:\n" + clip("\n\n".join(details), 9000))
    if browser:
        lines.append(
            "BROWSER ERRORS (uncaught in the page during the tests; page, then message):\n"
            + "\n".join(f"- {line}" for line in list(dict.fromkeys(browser))[:10])
        )
    if violations:
        lines.append("RULE VIOLATIONS (rule 10):\n" + "\n".join(violations[:30]))
    return ok and not violations, "\n".join(lines)
