"""Fixtures that keep the tests off the real machine.

Two rules hold everywhere below:

  * no test may read the developer's own environment or write into the real job registry, so
    every variable the package reads is cleared and the state directory is a tmp_path;
  * no unit test may need CASINO. What stands in for `runqmc` is a shell script in
    `fake_runqmc`, which is enough to exercise the launcher, the process group and the
    exit code -- the parts that are actually ours.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parent / 'data'
READ_ENV = ('CASINO_HOME', 'CASINO_ARCH', 'CASINO_RUNQMC', 'CASINO_MCP_STATE_DIR', 'CASINO_MCP_FORBID', 'XDG_STATE_HOME')

# The calculations the integration suite reads. They live in this repository and nowhere else:
# installing casino-mcp does not install PyCasino, so nothing here may point at its examples.
# The 18 are a settings cover -- one calculation per distinct runtype, basis, optimiser and
# wavefunction option -- so a clean checkout has something of every kind to parse and re-run.
EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


@pytest.fixture(autouse=True)
def isolated(request, tmp_path, monkeypatch):
    """No inherited environment, a private state directory, a cwd of its own.

    An integration test keeps the environment: it drives the real CASINO, and `runqmc` and
    `envmc` need $CASINO_HOME and $CASINO_ARCH to find anything. It still gets its own state
    directory, so it cannot write into the developer's job registry.
    """
    if request.node.get_closest_marker('integration') is None:
        for name in READ_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    else:
        monkeypatch.setenv('CASINO_MCP_STATE_DIR', str(tmp_path / 'state' / 'casino-mcp'))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def out_file():
    """A fixture `out` by name: tests/data/<name>/out, straight from CASINO's examples."""

    def get(name: str) -> Path:
        path = DATA / name / 'out'
        assert path.is_file(), f'missing test data {path}'
        return path

    return get


@pytest.fixture
def workdir(tmp_path):
    """A directory that looks enough like a calculation for `casino_run` to accept it."""
    path = tmp_path / 'calc'
    path.mkdir()
    (path / 'input').write_text('#-------------------#\n# CASINO input file #\n#-------------------#\nruntype : vmc\n')
    return path


@pytest.fixture
def fake_runqmc(tmp_path, monkeypatch):
    """A stand-in for runqmc, installed as $CASINO_RUNQMC.

    It writes an `out`, then exits with the code it is told to: it ignores runqmc's own
    arguments the way the real thing ignores ours, and sleeps when asked so that a test can
    catch it running and stop it.
    """

    def build(exit_code: int = 0, sleep: float = 0.0, out_text: str = 'fake CASINO out\n') -> Path:
        script = tmp_path / 'runqmc'
        script.write_text(
            '#!/bin/sh\n'
            f'printf %s "$*" > runqmc.args\n'
            f'printf %b {out_text!r} > out\n'
            f'touch .runqmc.lock\n'
            f'sleep {sleep}\n'
            f'rm -f .runqmc.lock\n'
            f'exit {exit_code}\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv('CASINO_RUNQMC', str(script))
        return script

    return build


@pytest.fixture
def python_path(monkeypatch):
    """Let a spawned `python -m casino_mcp.launcher` import the package under test."""
    src = str(Path(__file__).resolve().parents[1] / 'src')
    existing = os.environ.get('PYTHONPATH', '')
    monkeypatch.setenv('PYTHONPATH', f'{src}{os.pathsep}{existing}' if existing else src)
    return sys.executable
