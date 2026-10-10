"""Filesystem boundary for generated commands, including their child processes.

Linux uses Landlock ABI 3+ (docs.kernel.org/userspace-api/landlock.html).
macOS uses its installed sandbox-exec. Unsupported hosts refuse execution unless AGENT_SANDBOX=0 turns the
boundary off; the agent then still works on a copy of the workspace without platform artifacts (main.py).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ENABLED = os.environ.get("AGENT_SANDBOX", "1") != "0"  # AGENT_SANDBOX=0 runs commands without the boundary


def command(argv: list[str], writable: list[Path], readable: Iterable[Path] = ()) -> list[str]:
    """Run in a separate process: preexec_fn is unsafe in the threaded agent."""
    if not ENABLED:
        return argv
    policy = {"write": [str(p.resolve()) for p in writable], "read": [str(p.resolve()) for p in readable]}
    return [sys.executable, str(Path(__file__).resolve()), json.dumps(policy), *argv]


def runtime_paths() -> list[Path]:
    paths = [
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc/alternatives",
        "/etc/ssl/certs",
        "/etc/ssl/openssl.cnf",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/gai.conf",
        "/etc/localtime",
        "/etc/os-release",
        "/dev/null",
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
    ]
    if sys.platform == "darwin":
        paths += ["/System", "/Library", "/Applications/Xcode.app", "/opt/homebrew", "/dev/fd", "/private/var/db/dyld"]
    else:
        paths += [
            "/proc/self",
            "/proc/thread-self",
            "/proc/meminfo",
            "/proc/cpuinfo",
            "/proc/stat",
            "/proc/uptime",
            "/proc/sys/kernel/osrelease",
            "/proc/sys/fs/inotify/max_user_watches",
            "/sys/devices/system/cpu",
        ]
    paths += [
        sys.prefix,
        sys.base_prefix,
        "/etc/fonts",
        "/var/cache/fontconfig",
        "/ms-playwright",
        str(Path.home() / ".cache/ms-playwright"),
        str(Path.home() / "Library/Caches/ms-playwright"),
    ]
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        paths.append(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    return [Path(p).resolve() for p in paths if Path(p).exists()]


def landlock(readable: list[Path], writable: list[Path]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def call(number, *args):
        result = libc.syscall(number, *args)
        if result < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        return result

    abi = call(444, 0, 0, 1)  # landlock_create_ruleset: query ABI version
    if abi < 3:
        raise RuntimeError(f"Landlock ABI 3+ required, found {abi}")
    handled = (1 << 15) - 1  # ABI 3 includes truncate and cross-directory rename/link
    read = (1 << 0) | (1 << 2) | (1 << 3)  # execute, read file, read directory
    write = handled & ~((1 << 6) | (1 << 11))  # never create device nodes
    file_rights = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
    ruleset = ctypes.c_uint64(handled)
    fd = call(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    try:
        # Browsers open /proc before inspecting their own descriptors. Other processes' protected
        # procfs data remains subject to Landlock's implicit ptrace restriction.
        for paths, rights in ((readable, read), (writable, write), ([Path("/proc")], 1 << 3)):
            for path in dict.fromkeys(paths):
                parent = os.open(path, os.O_PATH | os.O_CLOEXEC)  # type: ignore[attr-defined]  # O_PATH is Linux only
                try:
                    rule = PathRule(rights if path.is_dir() else rights & file_rights, parent)
                    call(445, fd, 1, ctypes.byref(rule), 0)  # landlock_add_rule: path beneath
                finally:
                    os.close(parent)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        call(446, fd, 0)  # landlock_restrict_self; inherited across fork/exec
    finally:
        os.close(fd)


def main() -> None:
    policy, argv = json.loads(sys.argv[1]), sys.argv[2:]
    writable = [Path(p) for p in policy["write"]] + [Path("/dev/null")]
    readable = [Path(p) for p in policy["read"]] + runtime_paths()
    if sys.platform == "linux":
        landlock(readable, writable)
    elif sys.platform == "darwin":

        def paths(items):
            return " ".join(
                f"({'subpath' if p.is_dir() else 'literal'} {json.dumps(str(p))})" for p in dict.fromkeys(items)
            )

        profile = (
            "(version 1)(allow default)(deny file-read-data file-write*)"
            '(allow file-read-data (literal "/"))'  # dyld reads the root directory before exec
            f"(allow file-read-data {paths(readable + writable)})"
            f"(allow file-write* {paths(writable)})"
        )
        # Apple's compiler shims use confstr's temp directory, ignoring TMPDIR.
        temp = subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
        cache = str(Path(temp).resolve() / "xcrun_db")
        profile += f"(allow file-read-data file-write* (regex {json.dumps('^' + re.escape(cache) + '($|-)')}))"
        argv = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
    else:
        raise RuntimeError(f"no filesystem sandbox for {sys.platform}")
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"SANDBOX ERROR: command refused: {error}", file=sys.stderr)
        sys.exit(126)
