"""Re-run every example and check CASINO still prints everything parse_out reads out of it.

    pytest -m integration tests/integration/test_examples_rerun.py    # ~10 minutes

This is the test the committed `examples/` tree exists for. `test_examples_envmc.py` asks
whether the parser agrees with CASINO's own tool about files written years ago; this one asks
the question that goes stale -- whether a *newly built* CASINO still writes them that way.
Install a new release, run this, and a renamed line or a dropped block shows up as the field
that is no longer parsed, instead of as a `None` nobody noticed.

Only losses fail. A new CASINO that prints more is fine; one that prints less has silently
taken a number away from every consumer of `parse_out`.

Values are not compared, for two reasons. A new release may legitimately move the numbers, and
that is a physics question for a human rather than an assertion; and a fixed `random_seed` does
not by itself make every run reproducible -- an optimisation redistributes configurations
across MPI processes and lands somewhere slightly different each time.
`tools/refresh_examples.py` reports the differences, and adopts the new output when they have
been looked at.

The number of MPI processes has to match the committed runs, because CASINO prints per-process
quantities that a serial run has none of; NPROC below is what the tree was made with.
"""

import shutil
import sys
from pathlib import Path

import pytest
from conftest import EXAMPLES

from casino_mcp.parse_out import parse_out

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))

from refresh_examples import run, shape  # noqa: E402

pytestmark = pytest.mark.integration

NPROC = 4

CALCULATIONS = sorted(p.parent for p in EXAMPLES.rglob('out'))
IDS = [str(p.relative_to(EXAMPLES)) for p in CALCULATIONS]


@pytest.fixture(scope='module', autouse=True)
def requirements():
    if shutil.which('runqmc') is None:
        pytest.skip('needs CASINO runqmc on the PATH')


@pytest.mark.parametrize('directory', CALCULATIONS, ids=IDS)
def test_a_fresh_run_still_prints_what_the_parser_reads(directory, tmp_path):
    committed = parse_out(directory)
    if not committed['complete']:
        pytest.skip('an interrupted run cannot be reproduced: there is no keyword for stopping early')

    text, _, error = run(directory, NPROC)
    assert not error, f'runqmc failed: {error}'
    fresh_out = tmp_path / 'out'
    fresh_out.write_text(text)

    before, after = shape(committed), shape(parse_out(fresh_out))
    assert after['phases'] == before['phases']
    assert not before['keywords'] - after['keywords'], 'keywords CASINO no longer echoes'
    assert not before['fields'] - after['fields'], 'numbers CASINO no longer prints where parse_out looks'
