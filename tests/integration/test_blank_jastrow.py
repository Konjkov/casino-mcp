"""Every blank Jastrow this writes, put to CASINO itself.

    pytest -m integration tests/integration/test_blank_jastrow.py    # a few seconds

`runqmc --check-only` is no oracle here: it validates the input and never opens
`correlation.data`. `testrun : T` is -- CASINO then "reads input files, prints information and
stops", which means it runs the whole of `read_pjastrow`, imposes the cusp and no-duplication
constraints on the gamma array, checks that they hold, and exits, all in a fraction of a
second. A blank Jastrow is right when that says so, and not when our own `check` does.

The systems are the committed examples: an all-electron atom on each basis this can read a
geometry from, and a pseudo-atom, which is the case where the chi cusp must stay off.
"""

import shutil
import subprocess

import pytest
from conftest import EXAMPLES

from casino_mcp import correlation_data, input_file, runtime

pytestmark = pytest.mark.integration

# calculation -> the files it is given. Each is a directory this can start a chain from: an
# orbital file, and no correlation.data of ours to get in the way.
SYSTEMS = {
    'gaussian_ae': (EXAMPLES / 'gwfn' / 'Be' / 'MP2-CASSCF(2.4)' / 'cc-pVQZ' / 'CBCS' / 'Jastrow_emin', ('gwfn.data',)),
    'gaussian_pp': (EXAMPLES / 'ppotential_HF' / 'O' / 'HF' / 'aug-cc-pVQZ-CDF' / 'CBCS' / 'Backflow_emin', ('gwfn.data', 'o_pp.data')),
    'slater_ae': (EXAMPLES / 'stowfn' / 'He' / 'HF' / 'QZ4P' / 'CBCS' / 'Jastrow_varmin', ('stowfn.data',)),
}

TERMS = (('u',), ('u', 'chi'), ('u', 'chi', 'f'))


def source_for(system: str, tmp_path):
    """One example, stripped back to what an orbital code would have left: no Jastrow."""
    calculation, files = SYSTEMS[system]
    if not (calculation / 'input').is_file():
        pytest.skip(f'no example calculation at {calculation}')
    path = tmp_path / f'{system}-source'
    path.mkdir()
    for name in files:
        shutil.copy2(calculation / name, path / name)
    keywords = input_file.read(calculation / 'input')['keywords']
    # a test run of a vmc calculation: CASINO reads everything and stops before moving anything
    values = {name: keywords[name] for name in ('neu', 'ned', 'atom_basis_type') if name in keywords}
    (path / 'input').write_text(input_file.build('vmc', {**values, 'testrun': 'T', 'use_jastrow': 'T'}))
    return path


def casino_reads(workdir) -> str:
    runqmc = runtime.find_runqmc()
    if runqmc is None:
        pytest.skip('runqmc not found')
    result = subprocess.run([runqmc, '-p', '1'], cwd=workdir, capture_output=True, text=True, timeout=300, check=False)
    output = (workdir / 'out').read_text() if (workdir / 'out').is_file() else ''
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ERROR' not in output and 'errstop' not in output.lower(), output
    return output


@pytest.mark.parametrize('system', sorted(SYSTEMS))
@pytest.mark.parametrize('terms', TERMS, ids=['-'.join(terms) for terms in TERMS])
def test_casino_reads_the_jastrow_this_writes(system, terms, tmp_path):
    source = source_for(system, tmp_path)
    prepared = runtime.prepare(str(source), str(tmp_path / f'{system}-{len(terms)}'), jastrow=list(terms))
    assert 'error' not in prepared, prepared

    output = casino_reads(tmp_path / f'{system}-{len(terms)}')
    assert 'Finished reading Jastrow factor from correlation.data.' in output
    for term in terms:
        assert f'{term.capitalize()} term:' in output, f'CASINO read no {term} term'
    assert 'Not all coefficients supplied: rest assumed to be zero.' in output


@pytest.mark.parametrize('system', sorted(SYSTEMS))
def test_casino_takes_the_cutoffs_it_was_left_to_choose(system, tmp_path):
    """A cutoff of zero is a request for CASINO's own default, and `describe` says which."""
    source = source_for(system, tmp_path)
    workdir = tmp_path / f'{system}-cutoffs'
    prepared = runtime.prepare(str(source), str(workdir), jastrow=['u', 'chi', 'f'])
    assert 'error' not in prepared, prepared

    output = casino_reads(workdir)
    assert output.count('Using default cutoff length') == 3

    basis = input_file.read(workdir / 'input')['keywords']['atom_basis_type'].strip()
    geometry = correlation_data.read_geometry(workdir / input_file.ORBITAL_FILE[basis])
    settings = correlation_data.settings_for()
    taken = [float(line.split(':')[1]) for line in output.splitlines() if 'Cutoff             (optimizable)' in line]
    assert taken == [correlation_data.defaulted_cutoff(term, geometry, settings) for term in ('u', 'chi', 'f')]


def test_an_all_electron_atom_can_impose_the_chi_cusp(tmp_path):
    """The one setting a person is most likely to want changed, and CASINO is strict about it."""
    source = source_for('gaussian_ae', tmp_path)
    prepared = runtime.prepare(str(source), str(tmp_path / 'cusped'), jastrow=['u', 'chi'], jastrow_settings={'cusp_chi': 1})
    assert 'error' not in prepared, prepared
    assert 'Electron-nucleus cusp imposed in Jastrow Chi term' in casino_reads(tmp_path / 'cusped')


def test_a_pseudo_atom_is_refused_the_chi_cusp_before_casino_sees_it(tmp_path):
    """CASINO errstops on this one, and the refusal has to come first."""
    source = source_for('gaussian_pp', tmp_path)
    prepared = runtime.prepare(str(source), str(tmp_path / 'wrong'), jastrow=['u', 'chi'], jastrow_settings={'cusp_chi': 1})
    assert any('pseudo-atom' in problem for problem in prepared['problems']), prepared
    assert not (tmp_path / 'wrong').exists()
