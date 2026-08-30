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
import time
from pathlib import Path

import pytest

from casino_mcp import input_file, runtime

DATA = Path(__file__).resolve().parent / 'data'
READ_ENV = (
    'CASINO_HOME',
    'CASINO_ARCH',
    'CASINO_RUNQMC',
    'CASINO_HALTQMC',
    'CASINO_MCP_STATE_DIR',
    'CASINO_MCP_FORBID',
    'XDG_STATE_HOME',
)

# The calculations the integration suite reads. They live in this repository and nowhere else:
# installing casino-mcp does not install PyCasino, so nothing here may point at its examples.
# The 18 are a settings cover -- one calculation per distinct runtype, basis, optimiser and
# wavefunction option -- so a clean checkout has something of every kind to parse and re-run.
EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


def wait_for(predicate, timeout=30.0, interval=0.1):
    """Wait for something a spawned launcher does. Here rather than in one test module: a
    process is what both the runtime tests and the model tests need to catch mid-flight."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f'timed out after {timeout}s')


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
        # Clearing the variables is not enough for haltqmc: a developer with CASINO on PATH
        # would have `stop` run the real script over a tmp_path. `fake_haltqmc` puts one back.
        monkeypatch.setattr(runtime, 'find_haltqmc', lambda: None)
    else:
        monkeypatch.setenv('CASINO_MCP_STATE_DIR', str(tmp_path / 'state' / 'casino-mcp'))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def out_file():
    """A fixture `out` by name: tests/data/<name>/out, with the `input` that produced it."""

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


ORBITALS = """TITLE
 water

GEOMETRY
--------
Number of atoms:
         3
Atomic numbers for each atom:
         8         1         1
Valence charges for each atom:
 6.0000000000000E+00 1.0000000000000E+00 1.0000000000000E+00

BASIS SET
"""


@pytest.fixture
def orbitals_only(tmp_path):
    """A directory as an orbital code leaves it: a wave function, an input, and no Jastrow."""
    path = tmp_path / 'hf'
    path.mkdir()
    (path / 'gwfn.data').write_text(ORBITALS)
    (path / 'input').write_text(input_file.build('vmc', {'neu': '5', 'ned': '5', 'atom_basis_type': 'gaussian'}))
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
def fake_haltqmc(tmp_path, monkeypatch):
    """A stand-in for haltqmc, installed as $CASINO_HALTQMC, with its helper beside it.

    It lives in a directory of its own, because what `-u` needs is `haltqmc_update_input` on
    PATH -- the real script looks it up there and errstops without it -- and a test has to be
    able to take that away. It records the flags it was given and does the part of the real
    script the tests care about: the lock file goes and config.out becomes config.in.
    """

    def build(exit_code: int = 0, helper: bool = True) -> Path:
        bindir = tmp_path / 'casino-bin'
        bindir.mkdir(exist_ok=True)
        script = bindir / 'haltqmc'
        script.write_text(
            '#!/bin/sh\n'
            'printf %s "$*" > haltqmc.args\n'
            'command -v haltqmc_update_input > haltqmc.helper\n'
            'rm -f .runqmc.lock\n'
            'if [ -s config.out ] ; then mv config.out config.in ; fi\n'
            f'exit {exit_code}\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        if helper:
            update = bindir / 'haltqmc_update_input'
            update.write_text('#!/bin/sh\nexit 0\n')
            update.chmod(update.stat().st_mode | stat.S_IXUSR)
        else:
            # a developer with CASINO on PATH would otherwise supply the helper this asks to do without
            kept = [entry for entry in os.environ.get('PATH', '').split(os.pathsep) if entry and not (Path(entry) / 'haltqmc_update_input').exists()]
            monkeypatch.setenv('PATH', os.pathsep.join(kept))
        monkeypatch.setenv('CASINO_HALTQMC', str(script))
        monkeypatch.setattr(runtime, 'find_haltqmc', lambda: str(script))
        return script

    return build


@pytest.fixture
def python_path(monkeypatch):
    """Let a spawned `python -m casino_mcp.launcher` import the package under test."""
    src = str(Path(__file__).resolve().parents[1] / 'src')
    existing = os.environ.get('PYTHONPATH', '')
    monkeypatch.setenv('PYTHONPATH', f'{src}{os.pathsep}{existing}' if existing else src)
    return sys.executable
