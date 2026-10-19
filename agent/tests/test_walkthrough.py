"""Runs every HTTPie command in walkthrough.md, in order, and checks its expectations.

The document is the test: each ``http`` command in a ```bash block must be followed by
``# expect: <status>`` and optionally ``# body contains: <fragment>`` lines (compared
with all whitespace removed). Commands run through the real ``http`` CLI against a fresh
mock platform and a live agent service that uses a deterministic stand-in model.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_layer.config import Settings
from agent_layer.service import build_runtime, create_app
from mock_platform.app import AUDITOR_TOKEN, MAKER_TOKEN, MockPlatform
from mock_platform.seed import seed_family
from mock_platform.server import BackgroundServer
from tests.conftest import TODAY
from tests.walkthrough_llm import WalkthroughLLM

DOC = Path(__file__).resolve().parents[1] / "walkthrough.md"
HTTP = shutil.which("http")
INBOUND_TOKEN = "demo-inbound-token"

pytestmark = pytest.mark.skipif(HTTP is None, reason="HTTPie (`http`) is not installed")


@dataclass
class Step:
    line: int
    command: str
    status: int | None = None
    contains: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        parts = shlex.split(self.command)
        method = parts[1] if parts[1].isupper() else "GET"
        url = next(p for p in parts[1:] if p.startswith(":"))
        return f"L{self.line}-{method}-{url.split('/', 1)[-1]}"


def parse_walkthrough(text: str) -> list[Step]:
    steps: list[Step] = []
    in_bash = False
    pending: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_bash = stripped == "```bash" and not in_bash
            continue
        if not in_bash:
            continue
        if pending:
            pending.append(stripped.rstrip("\\").strip())
            if not stripped.endswith("\\"):
                steps.append(Step(start, " ".join(pending)))
                pending = []
            continue
        if stripped.startswith("http "):
            start = number
            if stripped.endswith("\\"):
                pending = [stripped.rstrip("\\").strip()]
            else:
                steps.append(Step(number, stripped))
        elif m := re.match(r"# expect: (\d{3})$", stripped):
            steps[-1].status = int(m.group(1))
        elif m := re.match(r"# body contains: (.+)$", stripped):
            steps[-1].contains.append(m.group(1))
    return steps


STEPS = parse_walkthrough(DOC.read_text())


def test_every_command_has_an_expectation():
    assert STEPS, "no http commands found in walkthrough.md"
    missing = [s.line for s in STEPS if s.status is None]
    assert not missing, f"commands without '# expect:' at lines {missing}"


@pytest.fixture(scope="module")
def servers():
    platform = MockPlatform(today=TODAY)
    seed_family(platform)
    platform_server = BackgroundServer(platform.app)
    platform_url = platform_server.start()

    settings = Settings(platform_base_url=platform_url, maker_token=MAKER_TOKEN, auditor_token=AUDITOR_TOKEN,
                        inbound_token=INBOUND_TOKEN)
    runtime = build_runtime(settings, maker_llm=WalkthroughLLM(), auditor_llm=WalkthroughLLM())
    agent_server = BackgroundServer(create_app(runtime))
    agent_url = agent_server.start()

    ports = {":8200": platform_url.removeprefix("http://"), ":8100": agent_url.removeprefix("http://")}
    yield ports
    agent_server.stop()
    platform_server.stop()


def _localize(arg: str, ports: dict[str, str]) -> str:
    for short, host in ports.items():
        if arg == short or arg.startswith(short + "/"):
            return host + arg[len(short):]
    return arg


@pytest.mark.parametrize("step", STEPS, ids=[s.id for s in STEPS])
def test_walkthrough_step(servers, step: Step):
    args = [_localize(a, servers) for a in shlex.split(step.command)[1:]]
    # --ignore-stdin: no tty here, so HTTPie would otherwise read stdin as the body.
    proc = subprocess.run(
        [HTTP, "--ignore-stdin", "--print=hb", "--pretty=none", "--timeout=30", *args],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"http failed (line {step.line}): {proc.stderr}"
    head, _, body = proc.stdout.partition("\r\n\r\n") if "\r\n\r\n" in proc.stdout else proc.stdout.partition("\n\n")
    status = int(head.split()[1])
    assert status == step.status, f"line {step.line}: expected {step.status}, got {status}\n{body[:800]}"
    compact = re.sub(r"\s+", "", body)
    for fragment in step.contains:
        assert re.sub(r"\s+", "", fragment) in compact, f"line {step.line}: {fragment!r} not in\n{body[:1500]}"
