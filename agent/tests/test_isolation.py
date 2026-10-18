"""The agent zone is isolated behind the platform API: no DB, no files, no keys.

Two layers are tested:

* static – the package source cannot reach a database, the filesystem, a subprocess
  or a key library, and only ``platform_api`` / ``llm`` do network I/O;
* runtime – inside the audit-hook sandbox, a full request runs end to end over HTTP,
  while file reads/writes, subprocesses, DB drivers and other hosts are refused.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mock_platform.app import AUDITOR_TOKEN, MAKER_TOKEN

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_layer"
FORBIDDEN_IMPORTS = {
    "sqlite3", "dbm", "psycopg", "psycopg2", "asyncpg", "sqlalchemy", "pymongo", "redis", "pymysql",
    "solana", "solders", "spl", "nacl", "subprocess", "shutil", "pathlib", "tempfile", "socket",
    "requests", "urllib.request", "http.client", "pickle", "shelve", "ctypes",
}
NETWORK_MODULES = {"httpx": {"platform_api.py"}, "anthropic": {"llm.py"}}
OS_ALLOWED = {"config.py", "sandbox.py", "__main__.py"}  # environment variables and the sandbox itself
SOCKET_ALLOWED = {"sandbox.py"}  # pre-resolves allowlisted hosts


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(), filename=str(p))) for p in SRC.rglob("*.py")]


def _imports(tree: ast.Module) -> set[str]:
    """Top-level names plus full dotted names, so 'urllib.request' can be forbidden
    while 'urllib.parse' (pure string handling) stays allowed."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            full = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            full = [node.module]
        else:
            continue
        for name in full:
            names.update({name, name.split(".")[0]})
    return names


@pytest.mark.parametrize("path,tree", _modules(), ids=lambda v: v.name if isinstance(v, Path) else "")
def test_no_forbidden_imports(path, tree):
    imports = _imports(tree)
    forbidden = FORBIDDEN_IMPORTS - ({"socket"} if path.name in SOCKET_ALLOWED else set())
    assert not imports & forbidden, path
    for module, owners in NETWORK_MODULES.items():
        if module in imports:
            assert path.name in owners, f"{path.name} imports {module}; only {owners} may do network I/O"
    if "os" in imports:
        assert path.name in OS_ALLOWED, f"{path.name} imports os"


@pytest.mark.parametrize("path,tree", _modules(), ids=lambda v: v.name if isinstance(v, Path) else "")
def test_no_file_access_calls(path, tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in {"open", "exec", "eval", "compile", "__import__"}:
                pytest.fail(f"{path.name}:{node.lineno} calls {fn.id}()")
            if isinstance(fn, ast.Attribute) and fn.attr in {
                "read_text", "write_text", "read_bytes", "write_bytes", "unlink", "remove", "system", "popen",
            }:
                pytest.fail(f"{path.name}:{node.lineno} calls .{fn.attr}()")


SANDBOX_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from agent_layer.sandbox import install_sandbox
    # The scripted fake LLM lives in tests/, outside the agent's read roots: allow it explicitly.
    install_sandbox(allowed_hosts=["127.0.0.1"], extra_read_roots=[sys.argv[3]])

    results = {}

    def attempt(name, fn):
        try:
            fn()
            results[name] = "allowed"
        except PermissionError as exc:
            results[name] = "blocked"
        except Exception as exc:
            results[name] = f"error: {type(exc).__name__}: {exc}"

    import os, socket
    attempt("read_etc_passwd", lambda: open("/etc/passwd").read())
    attempt("write_file", lambda: open(sys.argv[2], "w").write("x"))
    attempt("os_open_write", lambda: os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT))
    attempt("delete_file", lambda: os.remove(sys.argv[2]))
    attempt("list_root", lambda: os.listdir("/"))
    attempt("subprocess", lambda: __import__("subprocess").run(["true"]))
    attempt("os_system", lambda: os.system("true"))
    attempt("import_sqlite3", lambda: __import__("sqlite3"))
    attempt("import_solders", lambda: __import__("solders"))
    attempt("dns_other_host", lambda: socket.getaddrinfo("example.com", 443))
    attempt("connect_other_ip", lambda: socket.create_connection(("10.255.255.1", 5432), timeout=1))
    attempt("listen", lambda: socket.socket().bind(("127.0.0.1", 0)))

    # The real thing: a full request through the maker agent, over HTTP, inside the sandbox.
    from agent_layer.auditor.auditor import Auditor
    from agent_layer.maker.agent import MakerAgent
    from agent_layer.platform_api import AuditorAPI, MakerAPI
    from tests.fakes import ScriptedLLM, finish, reply, transfer

    base = sys.argv[1]
    maker = MakerAgent(
        ScriptedLLM([reply(transfer(10)), reply(finish("sent_full", "Sent!"))]),
        MakerAPI(base, "%(maker)s"),
        Auditor(ScriptedLLM(), AuditorAPI(base, "%(auditor)s")),
    )
    record = maker.handle_request("r1")
    results["agent_run"] = record.outcome.value
    print(json.dumps(results))
    """
) % {"maker": MAKER_TOKEN, "auditor": AUDITOR_TOKEN}


def test_runtime_sandbox_blocks_everything_but_the_api(platform, base_url, add_request, tmp_path):
    add_request(amount=10)
    target = tmp_path / "should_not_exist.txt"
    proc = subprocess.run(
        [sys.executable, "-c", SANDBOX_SCRIPT, base_url, str(target), str(Path(__file__).parent)],
        capture_output=True, text=True, timeout=60, cwd=Path(__file__).resolve().parents[1],
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])

    assert results.pop("agent_run") == "sent_full"
    assert results == {k: "blocked" for k in results}, results
    assert not target.exists()
    assert len(platform.sent) == 1  # the transfer went through the platform API
