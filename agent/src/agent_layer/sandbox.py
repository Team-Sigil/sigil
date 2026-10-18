"""In-process enforcement of the API boundary, using CPython audit hooks (PEP 578).

Once installed (it cannot be uninstalled), the agent process can:

* read files only under its own code and library roots (needed for imports),
* never write, delete or rename a file, or create a directory,
* never start a subprocess, load a native library via ctypes, or open SQLite,
* never import a database driver or a Solana key library,
* resolve and connect only to allowlisted hosts (the platform API and the Claude API),
  and listen only when explicitly allowed (to serve its own inbound API).

The model itself cannot run code; this guards against bugs, misbehaving dependencies
and future changes that would quietly cross the boundary. It is defence in depth, not
a replacement for container-level isolation (read-only filesystem, egress firewall,
no DB credentials in the agent's environment), which production should also use.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import sysconfig
from collections.abc import Iterable

FORBIDDEN_MODULES = frozenset(
    {
        "sqlite3", "_sqlite3", "dbm",
        "psycopg", "psycopg2", "asyncpg", "sqlalchemy", "pymongo", "redis", "pymysql", "mysql",
        "solana", "solders", "spl", "nacl",
    }
)

_WRITE_EVENTS = frozenset(
    {
        "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate",
        "os.symlink", "os.link", "os.utime", "os.chflags", "os.lchflags", "os.putenv", "os.unsetenv",
        "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move",
        "shutil.rmtree", "shutil.chown", "shutil.make_archive", "shutil.unpack_archive",
    }
)
_PROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
        "os.forkpty", "os.startfile", "pty.spawn", "os.kill", "os.killpg",
    }
)
_NATIVE_EVENTS = frozenset(
    {"ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function", "ctypes.cdata", "ctypes.string_at",
     "sqlite3.connect", "dbm.open", "webbrowser.open"}
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

_installed = False


class SandboxViolation(PermissionError):
    """Raised inside the offending call. A PermissionError, so code that tolerates
    ordinary permission failures (e.g. bytecode caching) keeps working."""


def _default_read_roots() -> list[str]:
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, "/usr/share/zoneinfo"}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path:
            roots.add(path)
    roots.update(p for p in sys.path if p and os.path.isdir(p))
    return sorted(roots)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def install_sandbox(
    *,
    allowed_hosts: Iterable[str],
    extra_read_roots: Iterable[str] = (),
    allow_listen: bool = False,
) -> None:
    """Install the audit hook for the rest of the process's life. Idempotent."""
    global _installed
    if _installed:
        return

    hosts = frozenset(h.lower() for h in allowed_hosts)
    ips: set[str] = {h for h in hosts if _is_ip(h)}
    for host in hosts - ips:  # resolve before the hook is active
        try:
            ips.update(info[4][0] for info in socket.getaddrinfo(host, None))
        except OSError:
            pass
    allowed_ips = frozenset(ips)
    read_roots = tuple(
        os.path.realpath(r) + os.sep for r in [*_default_read_roots(), *extra_read_roots]
    )
    sys.dont_write_bytecode = True

    def readable(path: object) -> bool:
        if isinstance(path, int):  # an already-open descriptor
            return True
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str):
            return False
        real = os.path.realpath(path)
        return any((real + os.sep).startswith(root) for root in read_roots)

    def deny(event: str, detail: object = "") -> None:
        raise SandboxViolation(f"agent sandbox blocked {event} {detail!r}")

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS
            )
            if writing:
                deny("file write", path)
            if not readable(path):
                deny("file read", path)
        elif event in ("os.listdir", "os.scandir"):
            if args and args[0] is not None and not readable(args[0]):
                deny(event, args[0])
        elif event in _WRITE_EVENTS:
            deny(event, args[:1])
        elif event in _PROCESS_EVENTS or event in _NATIVE_EVENTS:
            deny(event)
        elif event == "import":
            top = str(args[0]).split(".", 1)[0]
            if top in FORBIDDEN_MODULES:
                deny("import", args[0])
        elif event == "socket.getaddrinfo":
            host = args[0]
            if isinstance(host, bytes):
                host = host.decode(errors="replace")
            if host is not None and str(host).lower() not in hosts and str(host) not in allowed_ips:
                deny("dns lookup", host)
        elif event == "socket.connect":
            address = args[1]
            if not (isinstance(address, tuple) and str(address[0]) in allowed_ips):
                deny("connect", address)
        elif event == "socket.bind":
            if not allow_listen:
                deny("bind", args[1] if len(args) > 1 else "")

    sys.addaudithook(hook)
    _installed = True
