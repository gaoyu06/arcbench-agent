"""Coding agent: requirements directory in, tested React + Express app out.

    python3 main.py REQUIREMENTS_DIR --output-dir OUTPUT_DIR [--type web] [--resume]

One engineer session plans and builds the whole app in small tested slices in the output directory. It looks up
requirement text on demand and starts helper subagents in the background for work it does not need yet
(reviews, summaries, tests of later areas); their results reach it as messages, so nothing waits on them.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import checkpoint
import checks
import llm
import roles
import spec
from llm import FatalModelError
from tools import shell

HERE = Path(__file__).resolve().parent
MAP_SKIP = {"package-lock.json", "README.md", ".gitignore", "eslint.config.js"}
APP_PATHS = (
    "frontend",
    "backend",
    "PLAN.md",
    "PLAN-earlier.md",
    "README.md",
    ".gitignore",
    "package.json",
    "package-lock.json",
    ".agent",
    ".git",
)


@contextmanager
def isolated_workspace(destination: Path):
    """Copy application-owned paths; platform artifacts remain outside the agent's filesystem boundary.

    Publish changes at lead step boundaries and on exit, retaining partial progress if a run is interrupted.
    .git travels with the application so recorded checkpoints can still be resumed.
    """
    with tempfile.TemporaryDirectory(prefix="agent-work-") as directory:
        root = Path(directory)
        for name in APP_PATHS:
            source, target = destination / name, root / name
            if source.is_symlink():
                shutil.copy2(source, target, follow_symlinks=False)
            elif source.is_dir():
                shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".arc"))
            elif source.is_file():
                shutil.copy2(source, target)

        def files():
            found = {}
            for name in APP_PATHS:
                item = root / name
                paths = []
                if item.is_dir() and not item.is_symlink():
                    for directory, dirs, names in item.walk():
                        dirs[:] = [d for d in dirs if d not in {".arc", "node_modules"}]
                        paths.extend(directory / n for n in names)
                else:
                    paths.append(item)
                for path in paths:
                    try:
                        if path.is_symlink() or path.is_file():
                            stat = path.lstat()
                            found[path.relative_to(root)] = (stat.st_size, stat.st_mtime_ns)
                    except FileNotFoundError:
                        pass  # a helper renamed it while this checkpoint was being collected
            return found

        published = files()

        def publish():
            current = files()
            for path in published.keys() - current.keys():
                (destination / path).unlink(missing_ok=True)
                del published[path]
            for path, stamp in current.items():
                if published.get(path) == stamp:
                    continue
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # Replace atomically: Git may freshen a read-only object, and targets may be symlinks.
                    with tempfile.TemporaryDirectory(prefix=".publish-", dir=target.parent) as tmp:
                        staged = Path(tmp) / target.name
                        shutil.copy2(root / path, staged, follow_symlinks=False)
                        staged.replace(target)
                except FileNotFoundError:
                    print(f"[agent] publication deferred while a helper changed {path}", flush=True)
                    continue
                published[path] = stamp

        try:
            yield root, publish
        finally:
            publish()


def workspace_map(root: Path) -> str:
    """Paths and line counts of the template sources; the engineer reads the ones it needs."""
    lines = []
    for path in sorted(
        p
        for part in ("backend", "frontend")
        for p in (root / part).rglob("*")
        if p.is_file() and "node_modules" not in p.parts and "dist" not in p.parts
    ):
        if path.name in MAP_SKIP:
            continue
        lines.append(f"- {path.relative_to(root)} ({path.read_text(errors='replace').count(chr(10)) + 1} lines)")
    return "\n".join(lines)


def setup(root: Path) -> None:
    for item in (HERE / "template").iterdir():
        if not (root / item.name).exists():
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, root / item.name)
    if not (root / ".gitignore").exists():
        (root / ".gitignore").write_text(
            f".arc/\n{checkpoint.AGENT_DIR}/\nrequirements/\nnode_modules\ndist/\n*.db\n"
            "test-results/\nplaywright-report/\n"
        )
    shell("git init -q 2>/dev/null; git config user.email agent@local; git config user.name agent", root, 30)
    problem = checks.install(root)
    if problem:
        print(problem, flush=True)


# AGENT_TIME_LIMIT_MINUTES ends the build at that wall-clock time after start (0: no limit); 15 minutes before it
# the engineer is asked to stop starting work and finish with a whole-suite check.
TIME_LIMIT_MINUTES = float(os.environ.get("AGENT_TIME_LIMIT_MINUTES", "0"))
STEPS_PER_REQUIREMENT = 25  # the session stops after 4x this per requirement: a guard against a runaway loop


class Builder:
    def __init__(
        self,
        root: Path,
        req_dir: Path,
        tree: spec.Node,
        steps: int,
        earlier: bool = False,
        publish=None,
        deadline: float | None = None,
    ):
        self.root, self.req_dir, self.tree, self.steps, self.earlier = root, req_dir, tree, steps, earlier
        self.publish, self.deadline = publish, deadline
        self.run = checkpoint.RunState(root)
        self.status = spec.changes(tree, checkpoint.earlier_snapshot(root)) if earlier else {}

    def plan_text(self) -> str:
        path = self.root / roles.PLAN_FILE
        return path.read_text() if path.is_file() else "(not written yet)"

    def earlier_note(self) -> str:
        return roles.EARLIER_WORK.format(plan=roles.PLAN_FILE) if self.earlier else ""

    def task(self) -> str:
        return roles.BUILD_TASK.format(
            plan=roles.PLAN_FILE,
            root=self.root,
            req_dir=self.req_dir,
            template=workspace_map(self.root),
            earlier=self.earlier_note(),
            outline=spec.outline(self.tree, self.status),
        )

    def refreshed_task(self) -> str:
        return roles.REFRESH_TASK.format(
            plan=roles.PLAN_FILE,
            root=self.root,
            plan_text=self.plan_text(),
            earlier=self.earlier_note(),
            outline_code=roles.code_map(self.root),
            outline=spec.outline(self.tree, self.status),
        )

    def build(self, resume: bool) -> None:
        if self.run.done("build"):
            return
        engineer = roles.Engineer(
            roles.new_llm(),
            self.root,
            self.req_dir,
            self.tree,
            "engineer",
            self.steps,
            task_builder=self.refreshed_task,
            status=self.status,
            publish=self.publish,
            deadline=self.deadline,
        )
        engineer.run(self.task(), resume="last" if resume else None)
        checkpoint.save_snapshot(self.root, spec.snapshot(self.tree))  # what a later stage compares against
        self.run.finish("build", checkpoint.commit(self.root, "build finished"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("requirement_path", nargs="?", default=os.environ.get("ARCBENCH_TASK_DIR", "requirements"))
    parser.add_argument("--output-dir", default=os.environ.get("ARCBENCH_OUTPUT_DIR", "."))
    parser.add_argument("--type", default=os.environ.get("ARCBENCH_TASK_TYPE", "web"))
    parser.add_argument(
        "--steps",
        type=int,
        default=int(os.environ.get("AGENT_STEPS", "0")),
        help="step scale of the runaway guard; default 25 per atomic requirement",
    )
    parser.add_argument("--resume", action="store_true", help="continue an interrupted run in the output dir")
    args = parser.parse_args()
    started = time.time()
    req_dir, destination = Path(args.requirement_path).resolve(), Path(args.output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if (req_dir / ".arc").exists():
        print("[agent] refused: the requirement directory contains platform evaluation artifacts (.arc)", flush=True)
        return 1
    with isolated_workspace(destination) as (root, publish):
        return generate(args, req_dir, root, publish, started)


def generate(args, req_dir: Path, root: Path, publish, started: float) -> int:
    boundary = shell("true", root, 15, readable=[req_dir])
    if not boundary.rstrip().endswith("[exit 0]"):
        print(
            f"[agent] filesystem isolation unavailable; generation not started (AGENT_SANDBOX=0 runs without "
            f"it):\n{boundary}",
            flush=True,
        )
        return 1
    earlier = not args.resume and checkpoint.start_over(root)
    setup(root)
    if earlier:
        problem = checks.keep_stage_db(root)
        print(f"[agent] earlier stage's database: {problem or 'kept in ' + checks.STAGE_DB}", flush=True)
    llm.USAGE_LOG = root / checkpoint.AGENT_DIR / "usage.jsonl"
    run = checkpoint.RunState(root)
    if not run.done("template"):
        run.finish("template", checkpoint.commit(root, "template"))
    tree = spec.load(req_dir)
    builder = Builder(
        root,
        req_dir,
        tree,
        args.steps or STEPS_PER_REQUIREMENT * len(tree.atomics),
        earlier=earlier or (root / checkpoint.EARLIER_PLAN).exists(),
        publish=publish,
        deadline=started + TIME_LIMIT_MINUTES * 60 if TIME_LIMIT_MINUTES > 0 else None,
    )
    print(
        f"[agent] {len(tree.atomics)} atomic requirements; model {llm.DEFAULT_MODEL}"
        + (f"; to check against the app: {len(builder.status)}" if builder.earlier else ""),
        flush=True,
    )
    try:
        builder.build(args.resume)
    except FatalModelError as error:
        print(f"[agent] model unavailable, delivering current state: {error}", flush=True)
    print(f"[agent] finished in {time.time() - started:.0f}s", flush=True)
    report = llm.usage_report(llm.USAGE_LOG)  # whole run, including sessions before a resume
    (root / checkpoint.AGENT_DIR / "usage-report.txt").write_text(report + "\n")
    print(report, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
