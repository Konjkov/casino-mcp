"""Acceptance test for parse_out: every `out` in a real examples tree, against envmc.

    pytest -m integration --examples-dir ~/PycharmProjects/PyCasino/examples

envmc is the reference for what the VMC numbers in `out` mean, but it is not a copy of them:
its Fortran helper recomputes the block averages and the error bars, so envmc's error is its
own estimate and differs from the one CASINO printed -- by a per cent on good statistics, by a
factor when reblocking failed. What can therefore be asserted is:

  * the energy and the sample variance agree to the precision envmc printed them;
  * of the three error bars CASINO prints, the one parse_out reports as `error` is the one
    closest to envmc's -- i.e. the correlation-time row and not one of its neighbours;
  * the phase count and the CPU time agree.

The ratio between envmc's variance error and CASINO's is only reported, at the end of the
run. Reproducing it means reblocking from `vmc.hist`, which is a later stage and not a
parser's job.

Needs CASINO's `envmc` on the PATH and a tree of example calculations; without either, the
whole module skips. Note that `endmc` misparses numbers under a non-C locale, which is why
nothing here shells out to it.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from casino_mcp.parse_out import parse_out

pytestmark = pytest.mark.integration

FIGURES = 4
ROW = re.compile(r'E\s*=\s*(\S+)\s*;\s*var\s*=\s*(\S+)')
CPU = re.compile(r'Total CASINO CPU time\s*:::\s*(\S+)')
COMPACT = re.compile(r'^([-+]?[\d.]+)\((\d+)\)$')

ratios: list[float] = []


def pytest_generate_tests(metafunc):
    """One test per example `out`, so a failure names the calculation that produced it."""
    if 'out_path' not in metafunc.fixturenames:
        return
    root = metafunc.config.getoption('--examples-dir')
    paths = sorted(Path(root).expanduser().rglob('out')) if root else []
    metafunc.parametrize('out_path', paths, ids=[str(p.parent.relative_to(Path(root).expanduser())) for p in paths])


@pytest.fixture(scope='module', autouse=True)
def requirements(request):
    if not request.config.getoption('--examples-dir'):
        pytest.skip('needs --examples-dir (or $CASINO_EXAMPLES)')
    if shutil.which('envmc') is None:
        pytest.skip('needs CASINO envmc on the PATH')
    yield
    # the variance-error ratio is reported, never asserted: it is envmc's own estimate
    if ratios:
        reporter = request.config.pluginmanager.get_plugin('terminalreporter')
        reporter.write_line(f'\nvariance error, envmc / CASINO: min {min(ratios):.3f}  max {max(ratios):.3f}  n {len(ratios)}')


def decode(token):
    """envmc's `-2.86165255(9718)` as (value, error, quantum of the last printed digit)."""
    match = COMPACT.match(token)
    if not match:
        return None, None, None
    central, digits = match.group(1), match.group(2)
    decimals = len(central.split('.')[1]) if '.' in central else 0
    scale = 10.0**-decimals
    return float(central), int(digits) * scale, scale * 10 ** max(0, len(digits) - FIGURES)


def envmc(directory):
    run = subprocess.run(['envmc', '-nc', '-nf', str(FIGURES)], cwd=directory, capture_output=True, text=True, check=False)
    lines = run.stdout.split('\n')
    rows = [(m.group(1), m.group(2)) for m in (ROW.search(line) for line in lines) if m]
    cpu = next((float(m.group(1)) for m in (CPU.search(line) for line in lines) if m), None)
    return rows, cpu


def compare(name, ours, token, failures):
    central, error, quantum = decode(token)
    if central is None:
        return None, None
    if ours.get('value') is None:
        failures.append(f'{name}: envmc says {token}, parse_out says nothing')
        return None, None
    if abs(ours['value'] - central) > 0.6 * quantum:
        failures.append(f'{name}: {ours["value"]!r} vs envmc {central!r}')
    return error, quantum


def check_error_row(name, phase, reference, quantum, failures):
    """The reported error must be the correlation-time row: the one closest to envmc's.

    envmc rounds its own estimate, so a rival row within one printed digit of ours is a tie,
    and where CASINO reports bad reblock convergence envmc's estimate cannot adjudicate at all.
    """
    ours = phase['energy'].get('error')
    rows = {key: row['value'] for key, row in phase.get('energy_errors', {}).items()}
    if ours is None or reference is None or len(rows) < 2 or not phase['reblock_converged']:
        return
    closest = min(rows, key=lambda key: abs(rows[key] - reference))
    if abs(ours - reference) > abs(rows[closest] - reference) + quantum:
        failures.append(f'{name}: reported {ours!r}, but the {closest} row {rows[closest]!r} is closer to envmc {reference!r}')


def test_agrees_with_envmc(out_path):
    parsed = parse_out(out_path)
    rows, cpu = envmc(out_path.parent)
    failures = []

    vmc = [p for p in parsed['phases'] if p['kind'] == 'vmc' and p['energy']['value'] is not None]
    if len(vmc) != len(rows):
        if not parsed['complete']:
            # envmc rebuilds an energy from the blocks of an interrupted phase; parse_out
            # reports only what CASINO wrote, so there is nothing to compare.
            pytest.skip('interrupted run: envmc rebuilds an energy that CASINO never printed')
        pytest.fail(f'{len(vmc)} VMC phases parsed, envmc lists {len(rows)}')

    for i, (phase, (energy_token, variance_token)) in enumerate(zip(vmc, rows, strict=True), start=1):
        reference, quantum = compare(f'VMC #{i} energy', phase['energy'], energy_token, failures)
        check_error_row(f'VMC #{i} energy error', phase, reference, quantum or 0.0, failures)
        reference, quantum = compare(f'VMC #{i} variance', phase['variance'], variance_token, failures)
        ours = phase['variance'].get('error')
        if reference and ours:
            ratios.append(reference / ours)

    if cpu is not None and parsed['cpu_time']['value'] is not None and abs(parsed['cpu_time']['value'] - cpu) > 1e-6:
        failures.append(f'CPU time {parsed["cpu_time"]["value"]!r} vs envmc {cpu!r}')

    assert not failures, '\n'.join(failures)
