from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_RECIPE = """\
[project]
workspace = "single"
loader = "none"
[apps.main]
path = "."
profile = "vite"
resources = ["WEB_DEV_PORT"]
[resources.WEB_DEV_PORT]
type = "port"
range = [5174, 5200]
"""


def _run_fresh(code: str, cwd: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", dedent(code)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "action",
    [
        pytest.param("assert callable(sd.cli.main)", id="import"),
        pytest.param(
            """\
try:
    sd.main(["--help"])
except SystemExit as error:
    assert error.code == 0
""",
            id="help",
        ),
        pytest.param('assert sd.main(["completion", "bash"]) == 0', id="shellcode"),
        pytest.param(
            """\
import argcomplete
import io
import os
from functools import partial
output = io.StringIO()
os.environ.update(_ARGCOMPLETE="1", COMP_LINE="splash comp", COMP_POINT="11")
argcomplete.autocomplete = partial(
    argcomplete.autocomplete, exit_method=sys.exit, output_stream=output
)
try:
    sd.main([])
except SystemExit as error:
    assert error.code == 0
assert output.getvalue().strip() == "completion"
""",
            id="argcomplete",
        ),
        pytest.param(
            'assert sd.main(["hook", "post-checkout", "old", "new", "0"]) == 0',
            id="hook",
        ),
        pytest.param(
            """\
recipe = sd.Recipe.load(Path("splashdown.toml"))
text = sd.render_agent_guidance(Path.cwd(), recipe)
assert "npx vite --port" in text
""",
            id="render",
        ),
    ],
)
def test_unrelated_paths_leave_flyrail_unloaded(tmp_path, monkeypatch, action):
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text(_RECIPE)
    _run_fresh(
        "import sys\nfrom pathlib import Path\nimport splashdown as sd\n"
        + action
        + "\nassert not any(name == 'flyrail' or name.startswith('flyrail.') "
        "for name in sys.modules), sorted(name for name in sys.modules if 'flyrail' in name)\n",
        tmp_path,
    )


def test_guidance_lifecycle_loads_flyrail_in_fresh_processes(tmp_path, monkeypatch):
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text(_RECIPE)
    agents = tmp_path / "AGENTS.md"
    baseline = "# Team rules\n"
    agents.write_text(baseline)
    for command, status in [("update", "applied"), ("status", "current"), ("uninstall", "applied")]:
        output = _run_fresh(
            f"""\
            import sys
            import splashdown as sd
            assert 'flyrail' not in sys.modules
            assert sd.main(['--format', 'json', 'ai', {command!r}]) == 0
            assert 'flyrail' in sys.modules
            """,
            tmp_path,
        )
        payload = json.loads(output)
        assert payload["data"]["files"][0]["status"] == status
        if command == "update":
            assert "Framework: `vite`" in agents.read_text()
    assert agents.read_text() == baseline
