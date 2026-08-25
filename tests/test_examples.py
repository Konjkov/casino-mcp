"""The committed `examples/` tree: that all of it parses, and that it still spans what it must.

No CASINO is needed here. `tests/integration/test_examples_envmc.py` checks the same `out`
files against CASINO's own `envmc`; what is asserted below is blunter and always runs -- every
calculation parses, and the tree still covers the settings it was assembled to cover.

The tree is a cover, not a sample. Eighteen calculations were picked so that every runtype,
basis type, optimiser and wavefunction option appears at least once, together with the two
awkward files a parser gets wrong quietly: a run that never printed an energy, and one
interrupted between optimisation cycles. `COVER` is that intent written down -- drop the only
`vmc_dmc` run or the only periodic system and this fails, instead of narrowing what the parser
is exercised on without anyone noticing.
"""

import re

import pytest
from conftest import EXAMPLES

from casino_mcp.parse_out import parse_out

CALCULATIONS = sorted(p.parent for p in EXAMPLES.rglob('out'))
IDS = [str(p.relative_to(EXAMPLES)) for p in CALCULATIONS]

SETTING = re.compile(r'^\s*([a-z_0-9]+)\s*:\s*(\S+)')
WAVEFUNCTION = ('gwfn.data', 'stowfn.data', 'pwfn.data', 'bwfn.data', 'awfn.data')

# keyword -> the values that must appear somewhere in the tree
COVER = {
    'runtype': {'vmc', 'vmc_opt', 'vmc_dmc'},
    'atom_basis_type': {'gaussian', 'slater-type', 'plane-wave'},
    'periodic': {'T', 'F'},
    'psi_s': {'slater', 'geminal'},
    'complex_wf': {'F'},
    'use_jastrow': {'T', 'F'},
    'use_gjastrow': {'T'},
    'backflow': {'T', 'F'},
    'cusp_correction': {'F'},
    'vmc_method': {'1', '3'},  # electron-by-electron and configuration-by-configuration
    'opt_dtvmc': {'0', '1'},
    'opt_method': {'varmin', 'emin'},
    'opt_cycles': {'1', '4'},
    'opt_jastrow': {'T', 'F'},
    'opt_geminal': {'T', 'F'},
    'opt_det_coeff': {'T'},
    'opt_fixnl': {'T'},
    'vm_reweight': {'F'},
    'vm_filter': {'T'},
    'dmc_method': {'2'},
    'use_tmove': {'T', 'F'},
}


def settings(directory):
    """The keywords the `input` states, as CASINO reads them: last assignment wins."""
    found = {}
    for line in (directory / 'input').read_text().splitlines():
        match = SETTING.match(line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


@pytest.fixture(scope='module')
def tree():
    return {path: settings(path) for path in CALCULATIONS}


@pytest.mark.parametrize('directory', CALCULATIONS, ids=IDS)
def test_an_example_is_a_calculation_that_could_be_re_run(directory):
    """`out` alone would do for the parser, but then nobody could reproduce it."""
    assert (directory / 'input').is_file()
    assert any((directory / name).is_file() for name in WAVEFUNCTION), f'no wavefunction file in {directory}'
    assert not list(directory.glob('*.hist')), 'history files are large and no test reads them'


@pytest.mark.parametrize('directory', CALCULATIONS, ids=IDS)
def test_an_example_parses(directory):
    parsed = parse_out(directory)
    stated = settings(directory)

    assert parsed['runtype'] == stated['runtype']
    assert parsed['keywords'], 'no keyword block found'
    assert parsed['phases'], 'no phase found'
    if parsed['complete']:
        assert parsed['result']['energy']['value'] is not None
        assert parsed['cpu_time']['value'] is not None


def test_the_tree_spans_every_setting_it_was_assembled_to_span(tree):
    seen = {keyword: {found[keyword] for found in tree.values() if keyword in found} for keyword in COVER}
    missing = {keyword: sorted(wanted - seen[keyword]) for keyword, wanted in COVER.items() if wanted - seen[keyword]}
    assert not missing, f'no example left with these settings: {missing}'


def test_the_two_files_a_parser_gets_wrong_quietly_are_still_here(tree):
    unfinished = [parsed for parsed in map(parse_out, tree) if not parsed['complete']]

    assert unfinished, 'no interrupted run left: the parser must not invent an ending for one'
    assert [p for p in unfinished if 'energy' not in p['result']], 'no run left that stopped before printing any energy at all'


def test_a_pseudopotential_and_a_periodic_system_are_both_represented(tree):
    assert any(list(path.glob('*_pp.data')) for path in tree), 'no pseudopotential example left'
    assert any(found.get('periodic') == 'T' for found in tree.values()), 'no periodic example left'
