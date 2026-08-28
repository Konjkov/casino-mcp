"""Every recipe, put to CASINO itself.

    pytest -m integration tests/integration/test_recipes_check_only.py    # a few seconds

`runqmc --check-only` reads the input files, validates them and stops before computing
anything, which makes it the oracle worth having for `input_file`: a recipe is right when
CASINO says the input is runnable, not when our own `check` does. The unit suite asserts that
the two agree; this is what stops them agreeing only with each other.

The calculation each recipe is built for is one of the committed examples, so the orbital file
and the Jastrow factor beside the generated `input` are real ones.
"""

import shutil
import subprocess

import pytest
from conftest import EXAMPLES

from casino_mcp import input_file, runtime

pytestmark = pytest.mark.integration

# A finite all-electron atom with a Jastrow factor: the smallest thing every runtype can be
# written for.
SOURCE = EXAMPLES / 'stowfn' / 'He' / 'HF' / 'QZ4P' / 'CBCS' / 'Jastrow_varmin'

# What each runtype needs beyond the recipe's defaults before the input describes a run. These
# are the keywords a recipe deliberately does not guess -- the size of an optimisation sample
# and of a DMC population are the caller's to choose.
EXTRA: dict[str, dict[str, str | None]] = {
    'vmc_opt': {'vmc_nconfig_write': '1000'},
    'opt_vmc': {'vmc_nconfig_write': '1000'},
    # the source is a varmin calculation whose opt_plan runs two cycles, and `opt` is one
    'opt': {'opt_plan': None},
    'vmc_dmc': {'vmc_nstep': '128', 'vmc_nconfig_write': '128', 'dmc_target_weight': '128'},
    'vmc_dmc_equil': {'vmc_nstep': '128', 'vmc_nconfig_write': '128', 'dmc_target_weight': '128'},
}


@pytest.fixture(scope='module')
def source(tmp_path_factory):
    if not (SOURCE / 'input').is_file():
        pytest.skip(f'no example calculation at {SOURCE}')
    path = tmp_path_factory.mktemp('source')
    for name in ('input', 'stowfn.data', 'correlation.data'):
        shutil.copy2(SOURCE / name, path / name)
    # A dmc-only or `opt` runtype continues from a population an earlier run wrote. --check-only
    # does not read it, but runqmc tests the file for *size*, not existence, so an empty one
    # reads to it as no file at all.
    (path / 'config.in').write_text('not a real config file, but not an empty one either\n')
    return path


@pytest.mark.parametrize('runtype', sorted(input_file.RECIPES))
def test_casino_accepts_what_the_recipe_writes(runtype, source, tmp_path):
    runqmc = runtime.find_runqmc()
    if runqmc is None:
        pytest.skip('runqmc not found')

    prepared = runtime.prepare(str(source), str(tmp_path / runtype), runtype=runtype, overrides=EXTRA.get(runtype))
    assert 'error' not in prepared, prepared

    result = subprocess.run(
        [runqmc, '-p', '1', '--check-only'],
        cwd=prepared['workdir'],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert 'ERROR' not in output, output
