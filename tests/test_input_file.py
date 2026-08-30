"""What `input_file` promises: an edit that loses nothing, and a recipe that runs.

Two properties are worth more than any single assertion here and are tested from several
sides. The first is that `apply` is an *edit*: a keyword the module has never heard of, a
hand comment, a block -- all of them survive a rewrite, because a real calculation's `input`
is a document and not a generated file. The second is that `check` refuses exactly the
combinations CASINO refuses, and refuses them before a directory is written.

The `dmc_running` fixture is a real vmc_dmc input from PyCasino's examples tree; the
integration suite checks the module's output against CASINO's own `runqmc --check-only`.
"""

import pytest

from casino_mcp import input_file

pytestmark = pytest.mark.filterwarnings('error')

VMC = """#-------------------#
# CASINO input file #
#-------------------#

# Be molecule (ground state)

# SYSTEM
neu               : 2              #*! Number of up electrons (Integer)
ned               : 2              #*! Number of down electrons (Integer)
periodic          : F              #*! Periodic boundary conditions (Boolean)
atom_basis_type   : slater-type    #*! Basis set type (text)

# RUN
runtype           : vmc            #*! Type of calculation (Text)
newrun            : T              #*! New run or continue old (Boolean)

# VMC
vmc_equil_nstep   : 5000           #*! Number of equilibration steps (Integer)
vmc_nstep         : 10000000       #*! Number of steps (Integer)
vmc_nblock        : 1              #*! Number of checkpoints (Integer)
vmc_nconfig_write : 0              #*! Number of configs to write (Integer)
writeout_vmc_hist : F              #*! Write vmc.hist file in VMC (Boolean)

# GENERAL PARAMETERS
use_jastrow       : T              #*! Use a Jastrow function (Boolean)
"""

PERIODIC = """neu : 32
ned : 32
periodic : T
atom_basis_type : plane-wave
%block npcell
2 2 2
%endblock npcell
runtype : vmc
"""


# --- reading --------------------------------------------------------------------------


def test_keywords_and_blocks_are_read_apart():
    keywords, blocks = input_file.parse_text(PERIODIC)
    assert keywords['neu'] == '32'
    assert keywords['atom_basis_type'] == 'plane-wave'
    assert blocks == {'npcell': ['2 2 2']}
    assert 'npcell' not in keywords


def test_comments_and_case_do_not_confuse_the_reader():
    keywords, _ = input_file.parse_text('# runtype : dmc_dmc\nRunType : VMC   #*! Type of calculation\n')
    assert keywords == {'runtype': 'VMC'}


def test_read_accepts_a_directory(tmp_path):
    (tmp_path / 'input').write_text(VMC)
    assert input_file.read(tmp_path)['runtype'] == 'vmc'
    assert input_file.read(tmp_path) == input_file.read(tmp_path / 'input')


# --- editing --------------------------------------------------------------------------


def test_apply_changes_a_value_and_keeps_its_comment():
    text = input_file.apply(VMC, {'vmc_nstep': '1024'})
    assert 'vmc_nstep         : 1024           #*! Number of steps (Integer)' in text
    assert 'vmc_nstep         : 10000000' not in text


def test_apply_keeps_everything_it_was_not_asked_about():
    """The reason this edits instead of regenerating: nothing here comes out of a recipe."""
    text = input_file.apply(VMC, {'vmc_nstep': '1024'})
    assert '# Be molecule (ground state)' in text
    assert 'writeout_vmc_hist : F' in text, 'a keyword no recipe knows must survive a rewrite'
    assert text.count('runtype') == 1


def test_apply_adds_a_keyword_under_its_section():
    text = input_file.apply(VMC, {'dtvmc': '0.05'})
    lines = [line.split(':')[0].strip() for line in text.splitlines() if ':' in line and not line.startswith('#')]
    assert lines.index('dtvmc') > lines.index('vmc_nblock')
    assert lines.index('dtvmc') < lines.index('use_jastrow')


def test_apply_opens_a_section_the_file_does_not_have():
    text = input_file.apply(VMC, {'dtdmc': '0.01'})
    assert '\n# DMC\n' in text
    assert 'dtdmc             : 0.01' in text


def test_a_null_deletes_the_keyword():
    text = input_file.apply(VMC, {'writeout_vmc_hist': None})
    assert 'writeout_vmc_hist' not in text
    assert 'use_jastrow' in text


def test_a_block_survives_an_edit_that_does_not_name_it():
    text = input_file.apply(PERIODIC, {'runtype': 'vmc_opt'})
    assert '%block npcell\n2 2 2\n%endblock npcell' in text


def test_a_multi_line_value_is_written_as_a_block():
    text = input_file.apply(VMC, {'opt_plan': '1 method=varmin fix_cutoffs=T\n2\n3'})
    assert '%block opt_plan\n1 method=varmin fix_cutoffs=T\n2\n3\n%endblock opt_plan' in text


def test_setting_a_block_that_exists_replaces_it_whole():
    text = input_file.apply(PERIODIC, {'npcell': '4 4 4'})
    assert '%block npcell\n4 4 4\n%endblock npcell' in text
    assert '2 2 2' not in text


def test_deleting_a_block_takes_its_closing_line_too():
    text = input_file.apply(PERIODIC, {'npcell': None})
    assert 'npcell' not in text and '2 2 2' not in text


def test_an_edited_file_reads_back_as_what_was_asked_for():
    text = input_file.apply(VMC, {'runtype': 'vmc_dmc', 'dtdmc': '0.02', 'writeout_vmc_hist': None})
    keywords, _ = input_file.parse_text(text)
    assert keywords['runtype'] == 'vmc_dmc'
    assert keywords['dtdmc'] == '0.02'
    assert 'writeout_vmc_hist' not in keywords
    assert keywords['neu'] == '2'


# --- recipes --------------------------------------------------------------------------


def test_a_recipe_fills_what_the_runtype_needs_and_keeps_what_the_file_says():
    present, _ = input_file.parse_text(VMC)
    filled, missing = input_file.recipe('vmc_dmc', {'dtdmc': '0.02083'}, present=present)
    assert missing == []
    assert filled['runtype'] == 'vmc_dmc'
    assert filled['dtdmc'] == '0.02083'
    assert filled['dmc_target_weight'] == input_file.DMC['dmc_target_weight']
    assert 'neu' not in filled, 'the file already says how many electrons there are'
    assert 'vmc_nstep' not in filled, 'and how long the VMC phase is'


def test_a_recipe_from_nothing_names_what_only_the_caller_knows():
    _, missing = input_file.recipe('vmc', {})
    assert missing == ['neu', 'ned', 'atom_basis_type']


def test_build_writes_a_file_casino_would_accept():
    text = input_file.build('vmc_dmc', {'neu': '2', 'ned': '2', 'atom_basis_type': 'slater-type', 'dtdmc': '0.02083'}, title='Be atom')
    keywords, blocks = input_file.parse_text(text)
    assert input_file.check(keywords, blocks) == []
    assert '# Be atom' in text
    assert [section for section, _ in input_file.SECTIONS if f'# {section}\n' in text] == ['SYSTEM', 'RUN', 'VMC', 'DMC', 'GENERAL PARAMETERS']


def test_build_refuses_without_the_keywords_that_have_no_default():
    with pytest.raises(ValueError, match='neu, ned, atom_basis_type'):
        input_file.build('vmc', {})


def test_every_recipe_produces_an_input_that_passes_its_own_check():
    """A recipe that needs a fix before it runs is not a recipe."""
    system = {'neu': '2', 'ned': '2', 'atom_basis_type': 'slater-type'}
    for runtype in input_file.RECIPES:
        text = input_file.build(runtype, system)
        keywords, blocks = input_file.parse_text(text)
        assert input_file.check(keywords, blocks) == [], runtype
        assert keywords['runtype'] == runtype


def test_a_dmc_recipe_asks_for_the_population_statistics():
    """popstats : T is what gives a running DMC job a dmc.status to read; see parse_out."""
    keywords, _ = input_file.parse_text(input_file.build('vmc_dmc', {'neu': '1', 'ned': '1', 'atom_basis_type': 'gaussian'}))
    assert keywords['popstats'] == 'T'


# --- checking -------------------------------------------------------------------------


def test_a_dmc_run_that_would_start_from_too_few_configurations():
    keywords = {'runtype': 'vmc_dmc', 'vmc_nstep': '10000', 'vmc_nconfig_write': '16', 'dmc_target_weight': '1024'}
    problems = input_file.check(keywords)
    assert any('vmc_nconfig_write 16 is below dmc_target_weight 1024' in problem for problem in problems)


def test_a_vmc_phase_that_generates_configurations_has_to_write_some():
    problems = input_file.check({'runtype': 'vmc_dmc', 'vmc_nstep': '10000', 'vmc_nconfig_write': '0'})
    assert any('must be greater than zero' in problem for problem in problems)
    plain = {'runtype': 'vmc', 'vmc_nstep': '10000', 'vmc_nconfig_write': '0', 'neu': '1', 'ned': '1', 'atom_basis_type': 'gaussian'}
    assert input_file.check(plain) == [], 'a plain VMC run has no reason to write any'


def test_more_configurations_than_steps_to_write_them():
    problems = input_file.check({'runtype': 'vmc_opt', 'vmc_nstep': '1000', 'vmc_nconfig_write': '10000'})
    assert any('above vmc_nstep' in problem for problem in problems)


def test_the_mandatory_keywords_are_named_one_by_one():
    problems = input_file.check({'runtype': 'vmc_dmc', 'neu': '1', 'ned': '1', 'atom_basis_type': 'gaussian'})
    assert 'vmc_nstep is required for runtype vmc_dmc' in problems
    assert 'dmc_stats_nstep is required for runtype vmc_dmc' in problems
    assert 'dmc_equil_nstep is required for runtype vmc_dmc' in problems
    assert not any(problem.startswith('neu ') for problem in problems)
    # dtdmc and the *_nblock keywords have working defaults inside CASINO, so their absence is
    # not an error however much a run wants them set on purpose. `advise` is where they live.
    assert not any(problem.startswith(('dtdmc', 'dmc_stats_nblock', 'vmc_equil_nstep')) for problem in problems)


def test_both_dmc_step_counts_are_wanted_by_a_run_that_does_only_one_of_the_phases():
    """CASINO asks for the whole pair whichever half it is about to run; runqmc errstops without."""
    for runtype in ('dmc_equil', 'dmc_stats', 'vmc_dmc_equil'):
        problems = input_file.check({'runtype': runtype, 'neu': '1', 'ned': '1', 'atom_basis_type': 'gaussian'})
        assert f'dmc_equil_nstep is required for runtype {runtype}' in problems
        assert f'dmc_stats_nstep is required for runtype {runtype}' in problems


def test_a_short_equilibration_under_automatic_time_step_optimization():
    """opt_dtvmc is on by default and has a floor under vmc_equil_nstep. It is easy to walk into."""
    keywords = {'runtype': 'vmc', 'neu': '1', 'ned': '1', 'atom_basis_type': 'gaussian', 'vmc_nstep': '1000', 'vmc_equil_nstep': '50'}
    assert any('below the 250 that opt_dtvmc needs' in problem for problem in input_file.check(keywords))
    assert any('below the 2000' in problem for problem in input_file.check({**keywords, 'vmc_method': '3'}))
    assert input_file.check({**keywords, 'opt_dtvmc': '0'}) == []


def test_optimizing_something_that_is_switched_off():
    assert 'opt_backflow needs backflow : T' in input_file.check({'runtype': 'opt', 'opt_backflow': 'T', 'backflow': 'F'})
    assert 'opt_jastrow needs use_jastrow : T' in input_file.check({'runtype': 'opt', 'opt_jastrow': 'T'})


def test_a_plan_that_disagrees_with_the_cycle_count_is_advice_not_an_error():
    """CASINO resolves it silently -- `opt_cycles` becomes the block's line count -- so it runs."""
    keywords, blocks = {'runtype': 'vmc_opt', 'opt_cycles': '4'}, {'opt_plan': ['1 method=varmin', '2', '3']}
    assert not any('opt_plan' in problem for problem in input_file.check(keywords, blocks))
    assert any('the run will do 3' in note for note in input_file.advise(keywords, blocks))


def test_a_single_optimization_cannot_have_a_multi_cycle_plan():
    """`runtype : opt` is one cycle by definition, and CASINO errstops rather than truncating."""
    system = {'runtype': 'opt', 'neu': '1', 'ned': '1', 'atom_basis_type': 'gaussian'}
    assert any('single optimization' in problem for problem in input_file.check(system, {'opt_plan': ['1 method=varmin', '2']}))
    assert input_file.check(system, {'opt_plan': ['1 method=varmin']}) == []


def test_files_the_input_says_to_read(tmp_path):
    keywords = {'runtype': 'vmc', 'atom_basis_type': 'slater-type', 'use_jastrow': 'T'}
    problems = input_file.check_files(tmp_path, keywords)
    assert any('stowfn.data' in problem for problem in problems)
    assert any('correlation.data' in problem for problem in problems)

    (tmp_path / 'stowfn.data').touch()
    (tmp_path / 'correlation.data').touch()
    assert input_file.check_files(tmp_path, keywords) == []


def test_a_dmc_only_runtype_needs_the_configurations_it_continues_from(tmp_path):
    (tmp_path / 'gwfn.data').touch()
    keywords = {'runtype': 'dmc_stats', 'atom_basis_type': 'gaussian', 'use_jastrow': 'F'}
    assert any('config.in' in problem for problem in input_file.check_files(tmp_path, keywords))
    (tmp_path / 'config.in').write_text('configurations\n')
    assert input_file.check_files(tmp_path, keywords) == []


def test_configurations_left_under_the_name_a_finished_run_gives_them(tmp_path):
    """`runqmc` renames config.out to config.in itself, so refusing on its absence is too strict."""
    (tmp_path / 'gwfn.data').touch()
    (tmp_path / 'config.out').write_text('configurations\n')
    keywords = {'runtype': 'dmc_dmc', 'atom_basis_type': 'gaussian', 'use_jastrow': 'F'}
    assert input_file.check_files(tmp_path, keywords) == []
    assert input_file.configurations(tmp_path).name == 'config.out'


def test_an_empty_config_file_is_no_config_file(tmp_path):
    """runqmc tests these for size, not existence -- and then says 'not present' about a file
    that is sitting right there, which is worth knowing before believing the message."""
    (tmp_path / 'config.in').touch()
    assert input_file.configurations(tmp_path) is None


# --- advice ---------------------------------------------------------------------------


def test_steps_that_do_not_divide_into_blocks():
    notes = input_file.advise({'runtype': 'vmc_dmc', 'dmc_stats_nstep': '10000', 'dmc_stats_nblock': '3'})
    assert any('rounds it up' in note for note in notes)


def test_a_timestep_left_at_the_placeholder_is_called_out():
    notes = input_file.advise({'runtype': 'vmc_dmc', 'dtdmc': '0.01'})
    assert any('placeholder' in note for note in notes)
    assert not any('placeholder' in note for note in input_file.advise({'runtype': 'vmc_dmc', 'dtdmc': '0.02083'}))


def test_keywords_left_over_from_the_calculation_this_was_copied_from():
    notes = input_file.advise({'runtype': 'vmc_opt', 'dmc_stats_nstep': '10000', 'dtdmc': '0.01'})
    assert any('no DMC phase' in note and 'dmc_stats_nstep' in note for note in notes)


def test_opt_dtvmc_is_a_vmc_keyword_whatever_its_prefix_says():
    """The false positive that cost a campaign: `runtype : vmc` was told its step would not be read."""
    notes = input_file.advise({'runtype': 'vmc', 'opt_dtvmc': '0'})
    assert not any('opt_dtvmc' in note and 'will not read' in note for note in notes)
    # a real optimization keyword in the same file is still called out
    notes = input_file.advise({'runtype': 'vmc', 'opt_dtvmc': '0', 'opt_cycles': '4'})
    assert any('no OPT phase' in note and 'opt_cycles' in note and 'opt_dtvmc' not in note for note in notes)


def test_a_step_that_is_asked_for_and_then_optimized_away():
    """What ruins a scan over dtvmc: every point is optimized to the same ~50% acceptance step."""
    notes = input_file.advise({'runtype': 'vmc', 'dtvmc': '0.1'})
    assert any('optimizes the step away from it' in note for note in notes)
    assert not any('optimizes the step away' in note for note in input_file.advise({'runtype': 'vmc', 'dtvmc': '0.1', 'opt_dtvmc': '0'}))
    # and it is a VMC phase's business: a dmc-only runtype has no step to optimize
    assert not any('optimizes the step away' in note for note in input_file.advise({'runtype': 'dmc_stats', 'dtvmc': '0.1'}))


def test_a_pseudopotential_run_is_asked_about_tmove(tmp_path):
    (tmp_path / 'be_pp.data').touch()
    notes = input_file.advise_files(tmp_path, {'runtype': 'vmc_dmc'})
    assert any('use_tmove' in note and 'be_pp.data' in note for note in notes)


def test_tmove_without_a_pseudopotential_does_nothing(tmp_path):
    notes = input_file.advise_files(tmp_path, {'runtype': 'vmc_dmc', 'use_tmove': 'T'})
    assert any('does nothing without a pseudopotential' in note for note in notes)


# --- against the real thing -----------------------------------------------------------


def test_a_real_input_round_trips_unchanged(out_file):
    """Reading and writing back with nothing to change must be the identity."""
    path = out_file('dmc_running').parent / 'input'
    text = path.read_text()
    assert input_file.apply(text, {}) == text


def test_a_real_input_is_already_valid(out_file):
    """The fixture is a calculation that ran, so `check` must have nothing to say about it."""
    current = input_file.read(out_file('dmc_running').parent / 'input')
    assert input_file.check(current['keywords'], current['blocks']) == []
