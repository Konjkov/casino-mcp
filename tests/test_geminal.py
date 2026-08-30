"""What a written GEMINAL block has to be: a file CASINO reads, for the orbitals that exist.

Three properties carry this module, and they are the three ways the block can be wrong.

The *shape* is CASINO's, and the test for it is a committed example: the geminal calculations
under `examples/` hold `parameters.casl` files that were written by hand and then optimized, so
what this writes for the same system and the same channels has to name the same parameters and
impose the same constraint groups. Only the values differ, and they differ because the examples
hold optimized ones.

The *levels* are read off the orbital file, and a level that this ties component by component
had better be one. That is what `purity` decides, and a synthetic gwfn.data with one s and one p
shell pins the arithmetic that gets there.

The *refusals* come before the file. `psi_s : geminal` missing, more down- than up-spin
electrons, backflow with an open shell, a channel the orbital file has no level for: every one
of them is an errstop inside CASINO's `read_geminal`, and by then a queue slot has been spent.

The integration suite puts the files themselves to a real CASINO with `testrun : T`.
"""

import pytest
from conftest import EXAMPLES

from casino_mcp import geminal

pytestmark = pytest.mark.filterwarnings('error')

# One s shell and one p shell: four basis functions, four orbitals, and every orbital exactly
# one of them. The real thing carries the basis set in between, which none of this reads.
HEADER = """TITLE
 a synthetic atom

BASIC_INFO
----------
Periodicity:
         0
Spin unrestricted:
    .false.
Number of electrons per primitive cell:
         2

BASIS SET
---------
Number of shells per primitive cell
         2
Number of basis functions ('AO') per primitive cell
         4
Code for shell types (s/sp/p/d/f... 1/2/3/4/5...)
         1         3

ORBITAL COEFFICIENTS
------------------------
"""

# MO 1 is the s function; MOs 2, 3 and 4 are the p slots 0, +1 and -1, in that order. Ordered
# by slot with slot 0 last, that level is [3, 4, 2].
IDENTITY = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]


def gwfn(path, orbitals=None, header: str = HEADER):
    """A gwfn.data whose ORBITAL COEFFICIENTS are the given matrix, four to a line."""
    numbers = [f'{value:20.13E}' for row in (orbitals or IDENTITY) for value in row]
    lines = [''.join(numbers[i : i + 4]) for i in range(0, len(numbers), 4)]
    path.write_text(header + '\n'.join(lines) + '\n')
    return path


@pytest.fixture
def atom(tmp_path):
    return geminal.read_orbitals(gwfn(tmp_path / 'gwfn.data'))


# --- the channels ---------------------------------------------------------------------


def test_a_channel_is_a_letter_and_a_count():
    assert geminal.parse_channels(['p:2', 'd:1']) == ([(1, 2), (2, 1)], [])


def test_a_bare_letter_is_one_level():
    assert geminal.parse_channels(['p']) == ([(1, 1)], [])


def test_an_unknown_letter_is_refused():
    _, errors = geminal.parse_channels(['h:1'])
    assert 'no such channel' in errors[0]


def test_a_count_that_is_not_a_number_is_refused():
    _, errors = geminal.parse_channels(['p:some'])
    assert 'positive whole number' in errors[0]


# --- the basis functions --------------------------------------------------------------


def test_a_shell_code_becomes_its_basis_functions():
    assert geminal.basis_functions([1, 3]) == [(0, 0), (1, 0), (1, 1), (1, 2)]


def test_an_sp_shell_is_four_functions_and_not_three():
    """Code 2 is CRYSTAL's sp shell: one s and a p triple, which no `2l+1` describes."""
    assert geminal.basis_functions([2]) == [(0, 0), (1, 0), (1, 1), (1, 2)]


def test_an_unknown_shell_code_is_refused():
    with pytest.raises(ValueError, match='shell type code 9'):
        geminal.basis_functions([9])


def test_only_d_carries_the_premultiplied_constants():
    """`molden2qmc.py:d_normalize`: the solid-harmonic constants go into the d coefficients
    and not into the f and g ones, while the m-dependent factor goes into all of them."""
    assert geminal.normalization(0, 0) == 1.0
    assert geminal.normalization(1, 1) == 1.0
    assert geminal.normalization(2, 0) == 0.5
    assert geminal.normalization(2, 1) == pytest.approx(3.0 * (2.0 / 6) ** 0.5)
    assert geminal.normalization(3, 0) == 1.0


# --- reading the orbitals -------------------------------------------------------------


def test_the_orbitals_come_back_with_the_file(atom):
    assert atom['norb'] == 4
    assert atom['functions'] == [(0, 0), (1, 0), (1, 1), (1, 2)]
    assert atom['electrons'] == 2
    assert not atom['unrestricted']
    assert len(atom['orbitals']) == 1


def test_an_unrestricted_file_holds_both_spins(tmp_path):
    doubled = IDENTITY + IDENTITY
    path = gwfn(tmp_path / 'gwfn.data', doubled, HEADER.replace('.false.', '.true.'))
    assert len(geminal.read_orbitals(path)['orbitals']) == 2


def test_a_file_with_no_orbital_coefficients_is_not_one(tmp_path):
    (tmp_path / 'gwfn.data').write_text(HEADER.split('ORBITAL')[0])
    with pytest.raises(ValueError, match='not a gwfn.data'):
        geminal.read_orbitals(tmp_path / 'gwfn.data')


def test_a_truncated_coefficient_block_is_refused(tmp_path):
    (tmp_path / 'gwfn.data').write_text(HEADER + ' 1.0000000000000E+00\n')
    with fails('ORBITAL COEFFICIENTS section holds'):
        geminal.read_orbitals(tmp_path / 'gwfn.data')


def fails(message):
    return pytest.raises(ValueError, match=message)


# --- the levels -----------------------------------------------------------------------


def test_a_level_is_ordered_by_m_slot_with_slot_zero_last(atom):
    """The last member is the one that carries the value in Parameters, and slot 0 -- the
    component with no partner to be rotated into -- is the natural one to declare."""
    levels = geminal.mo_levels(atom)
    assert levels[0] == [([1], True)]
    assert levels[1] == [([3, 4, 2], True)]


def test_a_level_whose_orbitals_mix_two_components_is_not_closed(tmp_path):
    mixed = [row[:] for row in IDENTITY]
    mixed[1] = [0.0, 0.7, 0.7, 0.0]  # an orbital that is half one p component and half another
    levels = geminal.mo_levels(geminal.read_orbitals(gwfn(tmp_path / 'gwfn.data', mixed)))
    assert levels[1] == [([2, 3, 4], False)]


def test_the_purity_that_decides_it_is_a_setting(tmp_path):
    mixed = [row[:] for row in IDENTITY]
    mixed[1] = [0.0, 1.0, 0.2, 0.0]
    orbitals = geminal.read_orbitals(gwfn(tmp_path / 'gwfn.data', mixed))
    assert geminal.mo_levels(orbitals, purity=0.98)[1] == [([2, 3, 4], False)]
    assert geminal.mo_levels(orbitals, purity=0.90)[1] == [([3, 4, 2], True)]


def test_a_closed_level_is_tied_component_by_component_and_the_rest_only_on_the_diagonal(tmp_path):
    mixed = [row[:] for row in IDENTITY]
    mixed[1] = [0.0, 0.7, 0.7, 0.0]
    levels = geminal.mo_levels(geminal.read_orbitals(gwfn(tmp_path / 'gwfn.data', mixed)))
    shells, diagonal, errors, notes = geminal.select(levels, [(1, 1)])
    assert (shells, diagonal, errors) == ([], [[2, 3, 4]], [])
    assert 'diagonal only' in notes[0]


def test_a_channel_the_file_has_no_level_for_is_refused_and_says_what_there_is(atom):
    _, _, errors, _ = geminal.select(geminal.mo_levels(atom), [(2, 1)])
    assert 'd:1 was asked for and the orbital file has 0 d level(s)' in errors[0]
    assert 'Available: s:1, p:1' in errors[0]


# --- the block ------------------------------------------------------------------------


def section(neu, ned, shells=(), diag_shells=(), **overrides):
    settings = geminal.settings_for(overrides)
    occupied, unpaired, anchors = geminal.occupation(neu, ned, list(shells), list(diag_shells), settings)
    return geminal.geminal_section(occupied, unpaired, anchors, list(shells), list(diag_shells), settings)


def test_the_hartree_fock_geminal_is_the_occupied_diagonal():
    assert section(2, 2).splitlines() == [
        'GEMINAL:',
        '  Default g optimizability: fixed',
        '  Default c optimizability: fixed',
        '  Geminal 1:',
        '    Parameters:',
        '      c: [ 1.0, fixed ]',
        '      g_1,1: [ 1.0, fixed ]',
        '      g_2,2: [ 1.0, fixed ]',
    ]


def test_an_open_shell_gets_one_fixed_unpaired_column_per_singly_occupied_orbital():
    """`check_umat` errstops on an empty unpaired column, and `parse_umat_el` refuses an
    optimizable one: neither is a choice this can make."""
    assert '      u_3,1: [ 1.0, fixed ]' in section(3, 2).splitlines()
    assert '      u_4,2: [ 1.0, fixed ]' in section(4, 2).splitlines()


def test_every_geminal_gets_the_unpaired_columns_and_not_only_the_first():
    written = section(3, 2, shells=[[3, 4, 5]], mirror=1)
    assert written.count('u_3,1') == 3


def test_the_correlating_geminal_keeps_the_anchors_and_seeds_the_leading_channels():
    written = section(2, 2, shells=[[3, 4, 5], [7, 8, 9], [11, 12, 13]], anchors=[1, 2])
    assert '      g_5,5: [ -0.05, optimizable ]' in written
    assert '      g_9,9: [ -0.02, optimizable ]' in written
    assert '      g_13,13: [ 0.0, optimizable ]' in written
    assert written.count('[ 1.0, fixed ]') == 6  # the two anchors and the c of each geminal


def test_the_seeds_go_by_position_over_every_correlated_level():
    """A level tied on its diagonal alone is still a level, and a leading one still leads."""
    written = section(2, 2, diag_shells=[[3, 4, 5]], anchors=[1, 2])
    assert '      g_5,5: [ -0.05, optimizable ]' in written


def test_the_anchors_are_the_occupied_orbitals_no_correlated_level_holds():
    settings = geminal.settings_for()
    assert geminal.occupation(3, 3, [[3, 4, 5]], [], settings) == ([1, 2, 3], [], [1, 2])


def test_the_anchors_can_be_overruled():
    """Be's 2s pair is replaced by the p block rather than kept beside it, and only the caller knows."""
    settings = geminal.settings_for({'anchors': [1]})
    assert geminal.occupation(2, 2, [[3, 4, 5]], [], settings) == ([1, 2], [], [1])


def test_a_shell_is_tied_to_itself_component_by_component():
    written = section(2, 2, shells=[[3, 4, 5]], anchors=[1, 2])
    assert '    2^g_5,5=2^g_4,4=2^g_3,3' in written.splitlines()


def test_two_shells_of_the_same_size_are_tied_off_diagonally_both_ways_round():
    written = section(2, 2, shells=[[3, 4, 5], [7, 8, 9]], anchors=[1, 2])
    assert '    2^g_5,9=2^g_9,5=2^g_4,8=2^g_8,4=2^g_3,7=2^g_7,3' in written.splitlines()


def test_shells_of_different_sizes_have_no_counterparts_to_tie():
    written = section(2, 2, shells=[[3, 4, 5], [10, 11, 12, 13, 14]], anchors=[1, 2])
    assert 'g_5,14' not in written
    assert len([line for line in written.splitlines() if line.startswith('    2^')]) == 2


def test_the_mirror_geminal_is_the_second_one_tied_to_it_with_the_opposite_sign():
    written = section(2, 2, shells=[[3, 4, 5]], anchors=[1], mirror=1)
    assert '  Geminal 3:' in written
    assert '      c: [ -1.0, fixed ]' in written
    assert '    2^g_5,5=2^g_4,4=2^g_3,3=3^g_5,5=3^g_4,4=3^g_3,3' in written.splitlines()
    # the mirror holds no anchors of its own: everything it has comes from the constraints
    assert written.split('  Geminal 3:')[1].split('  Constraints:')[0].count('g_') == 0


def test_a_geminal_with_no_channels_has_no_constraints_block():
    assert 'Constraints' not in section(2, 2)


def test_the_provenance_line_is_a_casl_comment():
    """`read_casl_file` strips `#` comments, which is what makes the line free."""
    assert geminal.casl('GEMINAL:\n', 'written by a test').startswith('# written by a test\nGEMINAL:')


# --- the settings ---------------------------------------------------------------------


def test_an_unknown_setting_is_refused_by_name():
    with pytest.raises(KeyError, match='no such geminal setting: wobble'):
        geminal.settings_for({'wobble': 1})


def test_the_defaults_are_the_ones_the_examples_start_from():
    assert geminal.settings_for()['seed'] == -0.05
    assert geminal.settings_for({'seed': -0.19})['seed'] == -0.19


# --- what CASINO would refuse ---------------------------------------------------------


def check(keywords, channels=(), orbitals=None, occupied=(1,), unpaired=(), anchors=(), **overrides):
    settings = geminal.settings_for(overrides)
    return geminal.check(keywords, list(channels), orbitals, list(occupied), list(unpaired), list(anchors), settings)


GEMINAL_INPUT = {'neu': '2', 'ned': '2', 'psi_s': 'geminal'}


def test_a_correct_input_is_not_complained_about():
    assert check(GEMINAL_INPUT) == []


def test_an_input_that_does_not_ask_for_a_geminal_is_refused():
    """The block would be dead text in the file: CASINO reads it only for psi_s : geminal."""
    problems = check({**GEMINAL_INPUT, 'psi_s': 'slater'})
    assert any('psi_s : geminal' in problem for problem in problems)


def test_more_down_than_up_spin_electrons_is_refused():
    problems = check({**GEMINAL_INPUT, 'neu': '2', 'ned': '3'})
    assert any('Swap the two spin channels' in problem for problem in problems)


def test_an_open_shell_is_refused_backflow_and_a_complex_wave_function():
    keywords = {**GEMINAL_INPUT, 'neu': '3', 'backflow': 'T', 'complex_wf': 'T'}
    problems = check(keywords, unpaired=[3])
    assert sum('unpaired electron(s)' in problem for problem in problems) == 2


def test_a_closed_shell_may_have_backflow():
    assert check({**GEMINAL_INPUT, 'backflow': 'T'}) == []


def test_a_casl_jastrow_shares_the_file_and_is_refused():
    """`use_gjastrow` puts a JASTROW block in this same parameters.casl, and this writes neither."""
    problems = check({**GEMINAL_INPUT, 'use_gjastrow': 'T'})
    assert any('use_gjastrow' in problem for problem in problems)


def test_an_anchor_outside_the_orbital_pool_is_refused(atom):
    problems = check(GEMINAL_INPUT, orbitals=atom, anchors=[9])
    assert any('outside the 1..4' in problem for problem in problems)


def test_a_periodic_orbital_file_is_refused(atom):
    problems = check(GEMINAL_INPUT, orbitals={**atom, 'periodicity': 3})
    assert any('periodic' in problem for problem in problems)


# --- against the committed examples ---------------------------------------------------

# The geminal calculations in this repository, with the channels their `parameters.casl` was
# written for. They are the only oracle for the shape of the block that does not come from us.
COMMITTED = {
    'Be': (EXAMPLES / 'geminal' / 'Be' / 'HF' / 'cc-pVQZ' / 'EBES' / 'Jastrow_emin__2_geminal__next', (2, 2), ['p:4'], {'anchors': [1]}),
    'Ne': (
        EXAMPLES / 'geminal' / 'Ne' / 'HF' / 'cc-pVQZ' / 'EBES' / 'Jastrow_emin__extended',
        (5, 5),
        ['p:4', 'd:1'],
        {'anchors': [1, 2], 'mirror': 1},
    ),
    'B': (EXAMPLES / 'geminal' / 'B' / 'UHF' / 'cc-pVQZ' / 'EBES' / 'Jastrow_emin', (3, 2), [], {}),
}


def written_for(name: str) -> str:
    calculation, (neu, ned), channels, overrides = COMMITTED[name]
    settings = geminal.settings_for(overrides)
    orbitals = geminal.read_orbitals(calculation / 'gwfn.data')
    wanted, errors = geminal.parse_channels(channels)
    assert errors == []
    shells, diagonal, problems, _ = geminal.select(geminal.mo_levels(orbitals, purity=settings['purity']), wanted)
    assert problems == []
    occupied, unpaired, anchors = geminal.occupation(neu, ned, shells, diagonal, settings)
    return geminal.geminal_section(occupied, unpaired, anchors, shells, diagonal, settings)


def declared(text: str) -> set[str]:
    """Every parameter the file gives a value to, as `n^name`, and constrained ones as their group.

    A group holds its value once and CASINO overwrites the rest of it, so *which* member
    carries it is arbitrary -- this writes the level's slot-0 orbital and the committed Ne file
    writes the first orbital of its d level. Naming a group by its own smallest member makes the
    two comparable without making the comparison vacuous: the groups themselves are compared
    line for line by `constraint_groups`.
    """
    groups = {member: min(sorted(group)) for group in constraint_groups(text) for member in group}
    names, geminal_index = set(), 0
    for line in text.splitlines():
        if line.strip().startswith('Geminal '):
            geminal_index = int(line.strip().split()[1].rstrip(':'))
        elif ': [' in line:
            parameter = f'{geminal_index}^{line.split(":")[0].strip()}'
            names.add(groups.get(parameter, parameter))
    return names


def constraint_groups(text: str) -> set[frozenset[str]]:
    """The constraints as what they mean -- groups of parameters held equal -- and not as lines.

    The order of a group is arbitrary: `2^g_5,5=2^g_4,4=2^g_3,3` and `2^g_5,5=2^g_3,3=2^g_4,4`
    ask CASINO for the same thing, and the committed files were written by hand.
    """
    return {frozenset(line.strip().split('=')) for line in text.splitlines() if '^g_' in line}


@pytest.mark.parametrize('name', sorted(COMMITTED))
def test_the_committed_examples_are_what_this_writes_for_them(name):
    committed = (COMMITTED[name][0] / 'parameters.casl').read_text()
    written = written_for(name)
    assert constraint_groups(written) == constraint_groups(committed)
    assert declared(written) == declared(committed)


def test_the_be_example_names_the_orbitals_its_levels_are_made_of():
    """The one that would break silently: a level read off the wrong m-slots still writes a
    file CASINO accepts, and ties orbitals that are not each other's counterparts."""
    calculation = COMMITTED['Be'][0]
    levels = geminal.mo_levels(geminal.read_orbitals(calculation / 'gwfn.data'))
    assert [sorted(members) for members, _ in levels[1][:4]] == [[3, 4, 5], [7, 8, 9], [15, 16, 17], [40, 41, 42]]


def test_an_unrestricted_file_is_read_off_its_up_spin_orbitals():
    calculation, _, _, _ = COMMITTED['B']
    orbitals = geminal.read_orbitals(calculation / 'gwfn.data')
    assert orbitals['unrestricted'] and len(orbitals['orbitals']) == 2
    assert geminal.spin_check(orbitals, 0.98, [(1, 2)])[0].startswith('the up- and down-spin orbitals')
