"""File and shell tools exposed to the model. Paths are relative to the workspace root."""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import sandbox
from patch import parse_patch, update_text

OUTPUT_LIMIT = 12000
SECRET_ENV = ("OPENAI_API_KEY", "VISUAL_API_KEY", "ARCBENCH_COOKIE", "ARCBENCH_PASSWORD")
NPM_CACHE = Path(tempfile.mkdtemp(prefix="agent-npm-"))
atexit.register(shutil.rmtree, NPM_CACHE, ignore_errors=True)

APPLY_PATCH_DESCRIPTION = """Create, change, rename and delete files with a patch. The patch is a stripped-down, \
file-oriented diff in this envelope:

*** Begin Patch
[ one or more file sections ]
*** End Patch

Each file section starts with one of three headers:
*** Add File: <path> - create a file, or replace an existing file completely. Every following line is a + line \
(the initial content), including empty lines (a lone +).
*** Delete File: <path> - remove an existing file. Nothing follows.
*** Update File: <path> - change an existing file in place, optionally followed by *** Move to: <new path> to \
rename it.

An Update File section holds one or more changes. Each change is a run of lines, each starting with one \
character: a space for an unchanged context line, - for a line to remove, + for a line to add. Give about 3 \
context lines above and below each change, copied exactly from the file, so the location is unique. When that \
is not enough (similar code in several places), start the change with an @@ line naming the enclosing line, \
such as `@@ function ListPage() {` or `@@ router.post('/items', async (req, res) => {`: the change is then \
looked for after that line. Separate changes in one file with @@ lines; give them in file order. End a change \
that must apply at the end of the file with *** End of File. A change of only + lines is inserted right after \
its @@ line; ended by *** End of File instead, it is appended to the file. To replace a whole file, use Add File.

Example:
*** Begin Patch
*** Add File: frontend/src/hello.ts
+export const hello = 'Hello';
*** Update File: backend/src/app.js
@@ app.get('/api/health', (req, res) => {
-  res.json({ code: 200, message: 'Backend Ready' });
+  res.json({ code: 200, message: 'OK' });
 });
*** Delete File: frontend/src/old.ts
*** End Patch

Rules:
- Paths are relative to the workspace root; node_modules cannot be edited.
- Prefix every added line with +, also in a new file.
- Context and removed lines must match the file; differences in leading or trailing whitespace and typographic \
quotes or dashes are tolerated. A change whose lines match several places without an @@ line is refused.
- Put every change you already know, for several files, into one patch. Prefer Update File with small changes \
to replacing a whole existing file: every line you send costs output time.
- Each file section is written only if all changes in that section apply. Successful sections stay written, \
including earlier sections for the same file. The result lists successful sections and failed changes with \
the closest lines in the current file. Resend only the failures.
- Do not re-read a file after a successful patch to verify it: the result reports every failure."""

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "apply_patch",
        "description": APPLY_PATCH_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "The whole patch, from *** Begin Patch to *** End Patch."},
                "check": {
                    "type": "string",
                    "description": "Optional: run the check tool in this same step once every "
                    "file of the patch applied, with this spec pattern (e.g. 'sort.spec', or 'sort.spec|filter.spec'), "
                    "or 'all' for the whole suite; its report follows the patch result. Set it whenever your next "
                    "call would be check, instead of spending a separate reply on it.",
                },
            },
            "required": ["input"],
        },
    },
    {
        "name": "bash",
        "description": "Run a command with bash in the workspace root and return its combined stdout and "
        "stderr followed by [exit N]. Use it to read and search files (`sed -n '1,200p' f`, `nl -ba f | "
        "sed -n '80,140p'` for line numbers, `rg -n pattern dir`, `ls`, `git diff`; not python scripts that print "
        "large chunks) and to run tools such as npx tsc. Output longer than 12000 characters keeps its start and end "
        "with the middle omitted, so read large files in ranges. Read-only commands (cat, sed -n, grep, rg, ls, wc, "
        "git diff/show/log, ...) at the start of a reply run concurrently. The command's process group is killed when "
        "it ends or times out, so a server or browser lives only inside one command: a subagent may run such an "
        "experiment that way, on a free port; the lead runs the app only through the check tool. Commands may be "
        "limited to reading this workspace, the supplied requirements, system runtimes and an isolated tool cache. "
        "Write files in the workspace, or use $TMPDIR for scratch files discarded when the command ends.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command line."},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the command is killed; default 180, max 600.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "view_image",
        "description": "Look at an image file (.png, .jpg, .jpeg, .gif, .webp), such as a "
        "reference screenshot named in the requirements. The result is the image itself when your model can see "
        "images, otherwise a detailed description of it (layout, every visible control with its exact label text, "
        "states) written by a vision model.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image path, relative to the workspace root or to the "
                    "requirements directory, or absolute.",
                }
            },
            "required": ["path"],
        },
    },
]


def clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    return f"{text[:head]}\n... [{len(text) - limit} chars omitted] ...\n{text[-(limit - head) :]}"


class Tools:
    def __init__(self, root: Path, readable: list[Path] | None = None, experiments: bool = True):
        self.root = root.resolve()
        self.readable = [self.root, *[p.resolve() for p in readable or []]]
        self.experiments = experiments  # False for the lead: debugging experiments belong in a subagent

    @property
    def schemas(self) -> list[dict]:
        return SCHEMAS

    def _path(self, path: str, *, write: bool) -> Path:
        target = (self.root / path).resolve()
        if not write and not target.exists():  # relative links in the requirements point into their directory
            target = next((found for base in self.readable[1:] if (found := (base / path).resolve()).exists()), target)
        allowed = [self.root] if write else self.readable
        if not any(target == base or base in target.parents for base in allowed):
            raise ValueError(f"path outside the workspace: {path}")
        if write and "node_modules" in target.relative_to(self.root).parts:
            raise ValueError("do not edit node_modules")
        return target

    def run(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
            return getattr(self, f"_{name}")(**args)
        except TypeError as error:  # arguments that do not match the schema, e.g. apply_patch without input
            schema: dict[str, Any] = next((s["parameters"] for s in SCHEMAS if s["name"] == name), {})
            return f"ERROR: {error}; {name} takes {json.dumps(schema.get('properties', {}))}"
        except Exception as error:  # the model sees every tool failure and decides what to do
            return f"ERROR: {type(error).__name__}: {error}"

    def _apply_patch(self, input: str, check: str | None = None) -> str:  # check: run by the session loop
        sections = parse_patch(input)
        done, failed = [], []
        for section in sections:
            try:
                target = self._path(section.path, write=True)
                moved = self._path(section.move, write=True) if section.move else None
                if section.kind == "add":
                    text = section.content
                elif not target.is_file():
                    raise ValueError(f"{section.path} does not exist")
                elif section.kind == "delete":
                    text = None
                else:
                    text = update_text(target.read_text(), section.chunks, section.path)
            except ValueError as error:
                failed.append(f"- {section.path}: {error}")
                continue
            if text is None:
                target.unlink()
                done.append(f"D {section.path}")
                continue
            destination = moved or target
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_name(destination.name + ".tmp")
            tmp.write_text(text)
            tmp.replace(destination)
            if moved and moved != target:
                target.unlink()
            done.append(f"{'A' if section.kind == 'add' else 'M'} {section.move or section.path}")
        written = "\n".join(done) or "nothing"
        if failed:
            return (
                f"ERROR: {len(failed)} of {len(sections)} file sections failed. Failed sections were not applied; "
                f"successful sections were written:\n{written}\n"
                f"The context and - lines must be copied from the file as it is now; "
                f"read the current text of these files (nl -ba <file> | sed -n 'a,bp') before resending their "
                f"patches:\n" + "\n".join(failed)
            )
        return f"Success. Updated the following files:\n{written}"

    def _bash(self, command: str, timeout: int = 180) -> str:
        if not self.experiments and EXPERIMENT.search(command) and not is_read_only(command):
            return (
                "ERROR: run the e2e tests through the check tool (pattern selects spec files). Starting the "
                "server, browser scripts and test runs by hand belong in a subagent: start one with the failing "
                "test, the error and the files involved, and let it run the experiments and report the cause."
            )
        return shell(command, self.root, min(max(timeout, 1), 600), readable=self.readable)


# Running the app or a browser by hand: e2e runs, server starts and browser scripts.
EXPERIMENT = re.compile(
    r"\bplaywright\s+test\b|\btest:e2e\b|\bchromium\.launch\b|\bnpm\s+(?:run\s+)?(?:start|dev)\b"
    r"|\bnode\s+(?:\S*/)?src/index\.js\b"
)
READ_ONLY = {
    "cat",
    "ls",
    "grep",
    "rg",
    "egrep",
    "head",
    "tail",
    "wc",
    "find",
    "sed",
    "sort",
    "uniq",
    "cut",
    "tr",
    "echo",
    "printf",
    "pwd",
    "tree",
    "stat",
    "file",
    "diff",
    "jq",
    "nl",
    "basename",
    "dirname",
    "realpath",
    "true",
    "cd",
    "git",
}
READ_ONLY_GIT = {"diff", "log", "show", "status", "grep", "ls-files", "rev-parse", "blame", "branch"}
UNSAFE = re.compile(
    r"(?<![0-9&])>(?!&)|>>|`|\$\(|\s-exec\b|\s-execdir\b|\s-ok\b|\s-delete\b|\s-fprint|\s-fls\b"
    r"|\btee\b|\s-i\b|--in-place|\s--output\b"
)
SED_ADDRESS = r"(?:\d+|\$|/[^/]*/)"
SED_PRINT = re.compile(
    rf"{SED_ADDRESS}(?:,{SED_ADDRESS})?p"
)  # sed scripts that only print; others may write (w) or run (e)


def is_read_only(command: str) -> bool:
    """True for commands made only of known read-only programs, without output redirection or substitution.

    Such commands may run concurrently with other read-only calls; anything unrecognised runs in order."""
    text = re.sub(r"\s[12]?>\s*/dev/null|\s2>&1", " ", command)
    if not text.strip() or UNSAFE.search(text):
        return False
    for segment in re.split(r"&&|\|\||;|\||\n", text):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if not words:
            continue
        if words[0] not in READ_ONLY:
            return False
        if words[0] == "sort" and any(w.startswith("-o") or w.startswith("--output") for w in words[1:]):
            return False
        if words[0] == "tree" and any(w.startswith("-o") for w in words[1:]):
            return False
        if words[0] == "sed" and (words[1:2] != ["-n"] or len(words) < 3 or not SED_PRINT.fullmatch(words[2])):
            return False
        if words[0] == "git" and (
            len(words) < 2 or words[1] not in READ_ONLY_GIT or (words[1] == "branch" and len(words) > 2)
        ):
            return False
    return True


def kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # group already gone (macOS reports EPERM)
        os.killpg(pid, signal.SIGKILL)


def shell(
    command: str,
    cwd: Path,
    timeout: int,
    env: dict | None = None,
    readable: list[Path] | None = None,
    writable: list[Path] | None = None,
    workspace: Path | None = None,
) -> str:
    """Run with a filesystem boundary and kill the process group before its scratch directory is removed."""
    clean = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    clean.update(env or {})
    workspace = workspace or cwd
    with (
        tempfile.TemporaryDirectory(prefix="agent-shell-") as scratch,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace", dir=scratch) as output,
    ):
        clean.update(
            TMPDIR=scratch,
            npm_config_cache=str(NPM_CACHE),
            npm_config_userconfig="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
        )
        argv = sandbox.command(
            ["bash", "--noprofile", "--norc", "-c", command],
            [workspace, Path(scratch), NPM_CACHE, *(writable or [])],
            readable or [],
        )
        proc = subprocess.Popen(
            argv, cwd=cwd, env=clean, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            proc.wait(timeout=timeout)
            status = f"exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            status = f"killed after {timeout}s timeout"
        finally:
            kill_group(proc.pid)
            proc.wait()
        output.seek(0)
        out = output.read()
    return clip(f"{out or ''}\n[{status}]")
