"""What a blank Jastrow has to be: a file CASINO reads, for the atoms that are actually there.

Two properties carry this module. The first is that the *shape* of the file is CASINO's --
every label, in the order `read_u_term`, `read_chi_term` and `read_f_term` consume them -- and
the test for it is a committed example: strip the parameter values out of a real
`correlation.data` and what is left has to be what this writes for the same system. The second
is that a refusal comes before a file: a cusp on a pseudo-atom, a periodic cell, a term for
atoms that do not exist.

The integration suite puts the generated files to a real CASINO with `testrun : T`, which reads
the input files and stops -- there is no equivalent of `runqmc --check-only` for this one,
because that script never opens `correlation.data`.
"""

import pytest
from conftest import EXAMPLES

from casino_mcp import correlation_data

pytestmark = pytest.mark.filterwarnings('error')

# A gwfn.data header, down to the two blank-separated sections `read_geometry` looks in. The
# real thing continues with the basis set for another few thousand lines.
GWFN = """TITLE
 a molecule

BASIC_INFO
----------
Periodicity:
         0
Number of electrons per primitive cell:
         16

GEOMETRY
--------
Number of atoms:
         3
Atomic positions (au):
 0.0000000000000E+00 0.0000000000000E+00 0.0000000000000E+00
 0.0000000000000E+00 0.0000000000000E+00 1.8100000000000E+00
 0.0000000000000E+00 1.8100000000000E+00 0.0000000000000E+00
Atomic numbers for each atom:
         8         1         1
Valence charges for each atom:
 6.0000000000000E+00 1.0000000000000E+00 1.0000000000000E+00

BASIS SET
---------
"""

# Eight atoms, whose numbers CASINO wraps onto a second line -- which is why the values are
# read by count and not by line.
WRAPPED = """GEOMETRY
--------
Number of atoms:
         8
Atomic numbers for each atom:
         5         5         1         1         1         1         1         1
Valence charges for each atom:
 5.0000000000000E+00 5.0000000000000E+00 1.0000000000000E+00 1.0000000000000E+00
 1.0000000000000E+00 1.0000000000000E+00 1.0000000000000E+00 1.0000000000000E+00
"""

PSEUDOPOTENTIAL = """ TM silicon pseudopotential
 Atomic number and pseudo-charge
 14 4.d0
 Energy units (rydberg/hartree/ev):
 rydberg
"""


@pytest.fixture
def molecule(tmp_path):
    (tmp_path / 'gwfn.data').write_text(GWFN)
    return correlation_data.read_geometry(tmp_path / 'gwfn.data')


# --- the geometry ---------------------------------------------------------------------


def test_atoms_come_out_of_the_orbital_file_header(molecule):
    assert molecule['periodicity'] == 0
    assert molecule['atomic_numbers'] == [8, 1, 1]
    assert molecule['valence_charges'] == [6.0, 1.0, 1.0]


def test_values_wrapped_over_several_lines_are_all_read(tmp_path):
    (tmp_path / 'gwfn.data').write_text(WRAPPED)
    geometry = correlation_data.read_geometry(tmp_path / 'gwfn.data')
    assert geometry['atomic_numbers'] == [5, 5, 1, 1, 1, 1, 1, 1]
    assert len(geometry['valence_charges']) == 8


def test_a_file_with_no_geometry_says_which_file(tmp_path):
    (tmp_path / 'gwfn.data').write_text('TITLE\nnot an orbital file at all\n')
    with pytest.raises(ValueError, match='gwfn.data'):
        correlation_data.read_geometry(tmp_path / 'gwfn.data')


def test_a_truncated_geometry_is_not_read_as_a_shorter_one(tmp_path):
    (tmp_path / 'gwfn.data').write_text('Number of atoms:\n   3\nAtomic numbers for each atom:\n   8   1\n')
    with pytest.raises(ValueError, match='found 2 of the 3'):
        correlation_data.read_geometry(tmp_path / 'gwfn.data')


def test_one_set_per_element_in_the_order_the_file_lists_them(molecule):
    assert molecule['sets'] == [{'z': 8, 'labels': [1]}, {'z': 1, 'labels': [2, 3]}]


def test_atoms_of_one_element_are_one_set_however_they_are_interleaved():
    assert correlation_data.species_sets([1, 8, 1]) == [{'z': 1, 'labels': [1, 3]}, {'z': 8, 'labels': [2]}]


def test_a_pseudopotential_states_its_own_atomic_number(tmp_path):
    (tmp_path / 'si_pp.data').write_text(PSEUDOPOTENTIAL)
    assert correlation_data.pseudo_species(tmp_path) == {14}


def test_a_directory_with_no_pseudopotential_has_no_pseudo_atoms(tmp_path):
    assert correlation_data.pseudo_species(tmp_path) == set()


# --- the file -------------------------------------------------------------------------


def test_what_is_written_is_what_a_real_correlation_data_says(molecule):
    """One atom of each element gets a set; the labels are the atoms' places in the file."""
    text = correlation_data.blank(molecule, title='water')
    assert ' START JASTROW' in text
    assert text.count(' START SET 1') == 3  # u, chi, f
    assert text.count(' START SET 2') == 2  # chi and f have a second element; u never has sets
    assert ' Number of sets ; labelling (1->atom in s. cell; 2->atom in p. cell; 3->species)\n   2 1\n' in text
    assert '    2    3' in text  # the two hydrogens, in one set
    assert text.rstrip().endswith('END JASTROW')


def test_no_parameter_values_are_written(molecule):
    text = correlation_data.blank(molecule)
    for line in text.splitlines():
        assert '!' not in line, line
    assert text.count('Parameter values') == 5  # u, and one per element in each of chi and f
    # every "Parameter values" line is immediately followed by the end of its set
    for index, line in enumerate(text.splitlines()):
        if 'Parameter values' in line:
            assert text.splitlines()[index + 1].strip().startswith('END SET')


def test_the_terms_asked_for_are_the_terms_written(molecule):
    text = correlation_data.blank(molecule, terms=('u',))
    assert 'START U TERM' in text
    assert 'CHI TERM' not in text and 'F TERM' not in text


def test_cutoffs_are_written_as_zero_which_casino_reads_as_its_own_default(molecule):
    text = correlation_data.blank(molecule)
    assert text.count('   0.0                               1') == 5
    notes = correlation_data.describe(molecule)
    assert any('cutoff_u 5.0' in note and 'cutoff_chi 4.0' in note and 'cutoff_f 3.0' in note for note in notes), notes


def test_a_cutoff_that_was_asked_for_is_written(molecule):
    text = correlation_data.blank(molecule, settings=correlation_data.settings_for({'cutoff_u': 8.0}))
    assert '   8.0                               1' in text
    assert not any('cutoff_u' in note for note in correlation_data.describe(molecule, settings=correlation_data.settings_for({'cutoff_u': 8.0})))


def test_a_single_atom_gets_casinos_shorter_default_cutoff(tmp_path):
    (tmp_path / 'gwfn.data').write_text(GWFN.replace('         3\n', '         1\n').replace('         8         1         1', '         4'))
    geometry = correlation_data.read_geometry(tmp_path / 'gwfn.data')
    assert correlation_data.defaulted_cutoff('u', geometry, correlation_data.settings_for()) == 2.0


def test_an_unknown_setting_is_refused_by_name():
    with pytest.raises(KeyError, match='n_you'):
        correlation_data.settings_for({'n_you': 8})


def test_the_committed_example_is_what_this_writes_for_the_same_atom():
    """A real `correlation.data`, minus its numbers, is what a blank one looks like."""
    calculation = EXAMPLES / 'gwfn' / 'Be' / 'MP2-CASSCF(2.4)' / 'cc-pVQZ' / 'CBCS' / 'Jastrow_emin'
    geometry = correlation_data.read_geometry(calculation / 'gwfn.data')
    # the JASTROW block alone: the example carries an MDET block after it, which is a wave
    # function this does not write and has nothing to do with the Jastrow factor
    committed = jastrow_lines((calculation / 'correlation.data').read_text())
    written = jastrow_lines(correlation_data.jastrow(geometry, title='Be atom'))
    # the cutoff values are the one place the two differ: the example holds optimized ones, and
    # a blank file leaves them at zero for CASINO to fill in
    assert written == committed


def jastrow_lines(text: str) -> list[str]:
    return block_lines(text, 'JASTROW')


def block_lines(text: str, block: str) -> list[str]:
    """One block: no blank lines, no parameter lines, and no cutoff *value*.

    The cutoff values are the one place a blank file and an optimized one legitimately differ --
    ours says zero and asks CASINO for its own default -- so they are dropped and everything else
    has to match line for line. eta's cutoffs go with them: they carry a `! L_s` comment, which
    is what marks a parameter line here.
    """
    lines = text.splitlines()
    start = lines.index(f' START {block}')
    kept, after_cutoff = [], False
    for line in lines[start : lines.index(f' END {block}') + 1]:
        if line.strip() and '!' not in line and not after_cutoff:
            kept.append(line)
        after_cutoff = line.strip().startswith('Cutoff (a.u.)')
    return kept


# --- what is refused ------------------------------------------------------------------


def test_a_periodic_system_is_refused_and_says_why(tmp_path):
    (tmp_path / 'gwfn.data').write_text(GWFN.replace('Periodicity:\n         0', 'Periodicity:\n         3'))
    geometry = correlation_data.read_geometry(tmp_path / 'gwfn.data')
    problems = correlation_data.check(geometry)
    assert any('make_p_stars' in problem for problem in problems), problems


def test_an_electron_nucleus_term_needs_nuclei():
    geometry = {'path': 'gwfn.data', 'periodicity': 0, 'atomic_numbers': [], 'valence_charges': [], 'sets': []}
    assert any('no atoms' in problem for problem in correlation_data.check(geometry))
    assert correlation_data.check(geometry, terms=('u',)) == []


def test_the_cusp_is_refused_on_a_pseudo_atom(molecule):
    settings = correlation_data.settings_for({'cusp_chi': 1})
    problems = correlation_data.check(molecule, settings=settings, pseudo={8})
    assert any('pseudo-atom' in problem for problem in problems), problems
    assert correlation_data.check(molecule, settings=settings, pseudo={14}) == []


def test_the_cusp_is_refused_on_a_slater_type_basis(molecule):
    settings = correlation_data.settings_for({'cusp_chi': 1})
    problems = correlation_data.check(molecule, settings=settings, basis='slater-type')
    assert any('slater-type' in problem for problem in problems), problems


def test_an_optimizable_cutoff_needs_a_truncation_order_casino_can_optimize(molecule):
    problems = correlation_data.check(molecule, settings=correlation_data.settings_for({'trunc_order': 1}))
    assert any('C >= 2' in problem for problem in problems), problems
    assert correlation_data.check(molecule, settings=correlation_data.settings_for({'trunc_order': 1, 'optimizable': 0})) == []


def test_an_expansion_order_below_what_casino_takes_is_refused(molecule):
    assert any('N_u 0' in problem for problem in correlation_data.check(molecule, settings=correlation_data.settings_for({'n_u': 0})))
    assert any('N_chi 0' in problem for problem in correlation_data.check(molecule, settings=correlation_data.settings_for({'n_chi': 0})))


def test_a_term_that_does_not_exist_is_named(molecule):
    assert any('eta' in problem for problem in correlation_data.check(molecule, terms=('u', 'eta')))


def test_an_all_electron_atom_on_a_cuspless_basis_is_pointed_out(molecule):
    notes = correlation_data.describe(molecule, basis='blip')
    assert any('all-electron' in note and 'cusp' in note for note in notes), notes
    assert not any('all-electron' in note for note in correlation_data.describe(molecule, basis='gaussian'))


# --- the backflow function ------------------------------------------------------------


def test_the_backflow_block_is_written_beside_the_jastrow(molecule):
    text = correlation_data.blank(molecule, backflow=correlation_data.BACKFLOW_TERMS)
    assert text.index(' START JASTROW') < text.index(' START BACKFLOW')
    assert ' END BACKFLOW' in text
    assert text.count(' START SET 2') == 4  # chi, f, mu and phi each have a second element


def test_either_block_can_be_written_alone(molecule):
    only_backflow = correlation_data.blank(molecule, terms=(), backflow=('eta',))
    assert 'JASTROW' not in only_backflow and ' START ETA TERM' in only_backflow
    only_jastrow = correlation_data.blank(molecule, terms=('u',))
    assert 'BACKFLOW' not in only_jastrow


def test_eta_gets_one_cutoff_line_per_spin_pair_channel(molecule):
    """The one cutoff in either file that is not a single number: `read_cutoff_eta` loops."""
    for spin_dep, channels in ((0, 1), (1, 2), (2, 3)):
        settings = correlation_data.settings_for({'spin_dep_eta': spin_dep})
        text = correlation_data.blank(molecule, terms=(), backflow=('eta',), settings=settings)
        assert [line for line in text.splitlines() if line.strip().startswith('0.0')] == [
            f'   0.0                               1       ! L_{channel}' for channel in range(1, channels + 1)
        ]


def test_the_backflow_cusp_type_is_read_off_the_pseudopotentials(molecule):
    """1 where the nucleus is bare, 0 where a pseudopotential stands in for it."""
    text = correlation_data.blank(molecule, terms=(), backflow=('mu', 'phi'), pseudo={8})
    types = [line.strip() for index, line in enumerate(text.splitlines()) if 'cusp conditions' in text.splitlines()[index - 1]]
    assert types == ['0', '1', '0', '1'], 'oxygen has a pseudopotential and the two hydrogens do not'


def test_the_cusp_type_can_be_set_by_hand_for_a_cuspless_orbital_set(molecule):
    settings = correlation_data.settings_for({'cusp_bf': 0})
    text = correlation_data.blank(molecule, terms=(), backflow=('mu',), settings=settings)
    assert '1' not in [line.strip() for index, line in enumerate(text.splitlines()) if 'cusp conditions' in text.splitlines()[index - 1]]
    assert any('by hand' in note for note in correlation_data.describe(molecule, terms=(), settings=settings, backflow=('mu',)))


def test_an_all_electron_phi_set_needs_the_expansion_order_casino_leaves_room_in(molecule):
    """N_eN of 1 leaves nothing free once the cusp conditions are imposed -- but only for AE."""
    settings = correlation_data.settings_for({'n_phi_en': 1})
    problems = correlation_data.check(molecule, terms=(), settings=settings, backflow=('phi',))
    assert any('no free parameters' in problem for problem in problems), problems
    assert correlation_data.check(molecule, terms=(), settings=settings, backflow=('phi',), pseudo={1, 8}) == []


def test_an_all_electron_mu_set_spends_one_parameter_per_channel_on_the_cusp(molecule):
    settings = correlation_data.settings_for({'n_mu': 1})
    assert any('no free parameters' in problem for problem in correlation_data.check(molecule, terms=(), settings=settings, backflow=('mu',)))
    assert correlation_data.check(molecule, terms=(), settings=settings, backflow=('mu',), pseudo={1, 8}) == []


def test_a_backflow_term_that_does_not_exist_is_named(molecule):
    problems = correlation_data.check(molecule, terms=(), backflow=('eta', 'theta'))
    assert any('theta' in problem for problem in problems), problems


def test_asking_for_neither_block_is_refused(molecule):
    assert any('empty file' in problem for problem in correlation_data.check(molecule, terms=(), backflow=()))


def test_the_backflow_cutoff_defaults_are_reported(molecule):
    notes = correlation_data.describe(molecule, terms=(), backflow=correlation_data.BACKFLOW_TERMS)
    assert any('cutoff_mu 4.5' in note and 'cutoff_phi 4.5' in note for note in notes), notes
    assert any('one line per spin-pair channel' in note for note in notes), notes


def test_the_ae_cutoffs_block_is_left_to_casino(molecule):
    text = correlation_data.blank(molecule, terms=(), backflow=('mu',))
    assert 'AE CUTOFFS' not in text
    assert any('AE CUTOFFS' in note for note in correlation_data.describe(molecule, terms=(), backflow=('mu',)))


def test_the_committed_example_is_what_this_writes_for_the_same_pseudo_atom():
    """That example is itself a blank backflow: a real one, and hand-written."""
    calculation = EXAMPLES / 'ppotential_HF' / 'O' / 'HF' / 'aug-cc-pVQZ-CDF' / 'CBCS' / 'Backflow_emin'
    geometry = correlation_data.read_geometry(calculation / 'gwfn.data')
    pseudo = correlation_data.pseudo_species(calculation)
    committed = block_lines((calculation / 'correlation.data').read_text(), 'BACKFLOW')
    written = block_lines(correlation_data.backflow_block(geometry, title='No title given.', pseudo=pseudo), 'BACKFLOW')
    # not to the column, and not to the wording of a line CASINO throws away: that file was
    # typed by a person, and it differs from `write_pbackflow` by a double space and by calling
    # one atom "Labels of the atoms". The reader reaches both by `read(io_bf,*)` with no variable
    assert [normalized(line) for line in written] == [normalized(line) for line in committed]


def normalized(line: str) -> str:
    return ' '.join(line.split()).replace('Label of the atom in this set', 'Labels of the atoms in this set')
