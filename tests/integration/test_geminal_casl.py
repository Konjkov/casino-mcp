"""Every `parameters.casl` this writes, put to CASINO itself.

    pytest -m integration tests/integration/test_geminal_casl.py    # about a minute

`runqmc --check-only` is no oracle here either: it validates the input and never opens
`parameters.casl`. `testrun : T` is -- CASINO then runs the whole of `read_geminal`, which
parses the block, resolves the constraint groups, checks them for contradictions, fills in the
defaults and calls `check_umat`, and stops before the first move. Everything that can be wrong
with the file is wrong by then.

The last test here is the one the manual asks for and no unit test can make: Geminal 1 with
g_m,m = 1 over the occupied orbitals *is* the Slater determinant, so a VMC run over it has to
land on the energy the same system gives with `psi_s : slater`. It is the only check that the
orbital indices this writes mean what it thinks they mean.
"""

import shutil
import subprocess

import pytest
from conftest import EXAMPLES

from casino_mcp import input_file, parse_out, runtime

pytestmark = pytest.mark.integration

# A closed shell and an open one. The open one is what makes the unpaired columns necessary:
# without a `u` line in every geminal, `check_umat` errstops.
SYSTEMS = {
    'closed': (EXAMPLES / 'gwfn' / 'Be' / 'MP2-CASSCF(2.4)' / 'cc-pVQZ' / 'CBCS' / 'Jastrow_emin', 2, 2),
    'open': (EXAMPLES / 'geminal' / 'B' / 'UHF' / 'cc-pVQZ' / 'EBES' / 'Jastrow_emin', 3, 2),
}

# Short, and with a seed, so the two wave functions of the last test are compared over the same
# walk length rather than over the same wall clock.
VMC = {'vmc_equil_nstep': '1024', 'vmc_nstep': '4096', 'vmc_nblock': '4', 'random_seed': 'standard', 'opt_dtvmc': '0', 'dtvmc': '0.05'}


def source_for(system: str, tmp_path, name: str = '', testrun: bool = True, psi_s: str = 'geminal'):
    """One example, stripped to what an orbital code leaves: an orbital file and an input."""
    calculation, neu, ned = SYSTEMS[system]
    if not (calculation / 'gwfn.data').is_file():
        pytest.skip(f'no example calculation at {calculation}')
    path = tmp_path / f'{system}-source{name}'
    path.mkdir()
    shutil.copy2(calculation / 'gwfn.data', path / 'gwfn.data')
    values = {'neu': str(neu), 'ned': str(ned), 'atom_basis_type': 'gaussian', 'psi_s': psi_s, 'use_jastrow': 'F'}
    (path / 'input').write_text(input_file.build('vmc', {**values, **VMC, 'testrun': 'T' if testrun else 'F'}))
    return path


def casino_reads(workdir) -> str:
    runqmc = runtime.find_runqmc()
    if runqmc is None:
        pytest.skip('runqmc not found')
    result = subprocess.run([runqmc, '-p', '1'], cwd=workdir, capture_output=True, text=True, timeout=600, check=False)
    output = (workdir / 'out').read_text() if (workdir / 'out').is_file() else ''
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ERROR' not in output and 'errstop' not in output.lower(), output
    return output


@pytest.mark.parametrize('system', sorted(SYSTEMS))
def test_casino_reads_the_hartree_fock_geminal_this_writes(system, tmp_path):
    source = source_for(system, tmp_path)
    workdir = tmp_path / f'{system}-hf'
    prepared = runtime.prepare(str(source), str(workdir), geminal=[])
    assert 'error' not in prepared, prepared

    output = casino_reads(workdir)
    assert 'Geminal setup' in output
    assert 'Using a pool of 55 orbitals.' in output


def test_an_open_shell_geminal_defines_its_unpaired_orbitals(tmp_path):
    """`check_umat` errstops on a geminal whose unpaired column is empty, and every geminal has one."""
    source = source_for('open', tmp_path)
    workdir = tmp_path / 'open-unpaired'
    prepared = runtime.prepare(str(source), str(workdir), geminal=['p:1'], geminal_settings={'mirror': 1})
    assert 'error' not in prepared, prepared
    assert (workdir / 'parameters.casl').read_text().count('u_3,1') == 3, 'one per geminal, not one per file'

    assert 'The wave function has 1 unpaired electron(s).' in casino_reads(workdir)


@pytest.mark.parametrize('channels,mirror', [(['p:2'], 0), (['p:4', 'd:1'], 0), (['p:2'], 1)], ids=['p2', 'p4-d1', 'p2-mirror'])
def test_casino_resolves_the_constraints_this_writes(channels, mirror, tmp_path):
    """Every tie is a group CASINO has to find consistent; a contradiction is an errstop."""
    source = source_for('closed', tmp_path, name=f'-{len(channels)}{mirror}')
    workdir = tmp_path / f'closed-{len(channels)}{mirror}'
    prepared = runtime.prepare(str(source), str(workdir), geminal=channels, geminal_settings={'anchors': [1], 'mirror': mirror})
    assert 'error' not in prepared, prepared

    output = casino_reads(workdir)
    assert 'Percentage of non-zero geminal matrix elements' in output


def test_the_hartree_fock_geminal_is_the_slater_determinant(tmp_path):
    """The manual's own check: g_m,m = 1 over the occupied orbitals gives the determinant back.

    Not digit for digit -- the geminal and the determinant code do not consume the random
    sequence alike, so the two walks differ -- but the energies are of the same wave function
    and have to agree inside their error bars.
    """
    geminal_dir, slater_dir = tmp_path / 'gem-vmc', tmp_path / 'slater-vmc'
    prepared = runtime.prepare(str(source_for('closed', tmp_path, name='-vmc', testrun=False)), str(geminal_dir), geminal=[])
    assert 'error' not in prepared, prepared
    shutil.copytree(source_for('closed', tmp_path, name='-slater', testrun=False, psi_s='slater'), slater_dir)

    energies = []
    for workdir in (geminal_dir, slater_dir):
        casino_reads(workdir)
        result = parse_out.parse_out(workdir / 'out')['result']
        energies.append((result['energy']['value'], result['energy']['error']))
    (gem, gem_error), (slater, slater_error) = energies
    assert abs(gem - slater) < 5 * (gem_error**2 + slater_error**2) ** 0.5, f'{energies}'


def test_more_down_than_up_spin_electrons_is_refused_before_casino_sees_it(tmp_path):
    """READ_GEMINAL errstops on it; the refusal has to come first."""
    source = source_for('closed', tmp_path)
    prepared = runtime.prepare(str(source), str(tmp_path / 'inverted'), overrides={'neu': '1', 'ned': '3'}, geminal=[])
    assert any('Swap the two spin channels' in problem for problem in prepared['problems']), prepared
    assert not (tmp_path / 'inverted').exists()
