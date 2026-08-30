"""What `parse_out` promises, against five real CASINO `out` files.

The values below were read out of the fixtures by hand. That is the point: a test that
recomputes them with the parser would only assert that the parser agrees with itself. The
line numbers are asserted too -- they are how a claim made from a parsed number is checked
against the file it came from, so a shifted line is a real defect, not a cosmetic one.

Each fixture keeps the `input` that produced it, so the numbers below can be reproduced rather
than trusted. They are five shapes, not five calculations: a single VMC, varmin, emin, a DMC
run split over many blocks, and a run that was killed. `examples/` is where breadth lives --
this is where precision does.

The heavier oracles live in tests/integration: the same parser against CASINO's own `envmc`,
and against a freshly built CASINO. This suite is the part that runs anywhere, in a second.
"""

import json

import pytest

from casino_mcp.parse_out import parse_dmc_status, parse_out, select

pytestmark = pytest.mark.filterwarnings('error')


def test_single_vmc(out_file):
    parsed = parse_out(out_file('vmc_single'))
    assert parsed['runtype'] == 'vmc'
    assert parsed['complete'] is True
    assert parsed['version']['value'].startswith('CASINO v')
    assert parsed['mpi_processes']['value'] == 4.0
    assert [phase['kind'] for phase in parsed['phases']] == ['vmc']

    result = parsed['result']
    assert result['phase'] == 0 and result['kind'] == 'vmc'
    assert result['energy'] == {'value': -2.862098498845, 'error': 0.00026604262, 'line': 237}
    assert parsed['cpu_time'] == {'value': 55.62, 'line': 245}


def test_single_block_variance_error_is_derived_and_labelled(out_file):
    """CASINO omits the sample-variance error for a one-block run; it is then the block's own."""
    parsed = parse_out(out_file('vmc_single'))
    phase = parsed['phases'][0]
    assert phase['nblock'] == 1
    variance = phase['variance']
    assert variance['value'] == 0.555497740165
    assert variance['error'] == phase['blocks'][0]['variance']['error']
    assert variance['derived'] == 'error taken from the only block, as envmc does'


def test_error_reported_is_the_correlation_time_row(out_file):
    """Of the three error bars CASINO prints, `error` must be the correlation-time one."""
    parsed = parse_out(out_file('vmc_single'))
    rows = parsed['phases'][0]['energy_errors']
    assert set(rows) == {'no_correction', 'correlation_time', 'reblock'}
    assert parsed['phases'][0]['energy']['error'] == rows['correlation_time']['value']


def test_vmc_opt_is_a_sequence_of_phases(out_file):
    """`vmc_opt` writes a VMC and an OPTIMIZATION phase per cycle -- never one result."""
    parsed = parse_out(out_file('vmc_opt_varmin'))
    assert parsed['runtype'] == 'vmc_opt'
    assert [phase['kind'] for phase in parsed['phases']] == ['vmc', 'opt', 'vmc', 'opt', 'vmc']
    assert [phase['index'] for phase in parsed['phases'] if phase['kind'] == 'opt'] == [1, 2]
    # `result` is the last phase carrying an energy: the post-fit VMC, not the optimization
    assert parsed['result']['phase'] == 4
    assert parsed['result']['energy']['value'] == -0.500095952582


def test_varmin_reports_a_variance_and_no_energy(out_file):
    """Not a gap in the parser: varmin's target is the variance, the energy is the next VMC phase."""
    parsed = parse_out(out_file('vmc_opt_varmin'))
    opt = parsed['phases'][1]
    assert opt['method']['value'] == 'varmin'
    assert opt['variance'] == {'value': 0.0012031461, 'line': 494}
    assert opt['energy']['value'] is None
    assert opt['energy']['reason'] == 'optimization phase reports no final energy'


def test_emin_reports_both(out_file):
    """An emin run opens with one varmin cycle; only the emin phases carry an energy."""
    parsed = parse_out(out_file('vmc_opt_emin'))
    methods = [phase['method']['value'] for phase in parsed['phases'] if phase['kind'] == 'opt']
    assert methods == ['varmin', 'emin', 'emin', 'emin']
    emin = parsed['phases'][3]
    assert emin['method']['value'] == 'emin'
    assert emin['energy']['value'] == pytest.approx(-0.5001462292988536)
    assert emin['energy']['error'] is not None
    assert emin['variance']['value'] is not None


def test_dmc_phases_and_mixed_estimators(out_file):
    parsed = parse_out(out_file('vmc_dmc'))
    assert [phase['kind'] for phase in parsed['phases']] == ['vmc', 'dmc_equil', 'dmc_stats']
    stats = parsed['phases'][2]
    assert stats['energy'] == {'value': -14.657147024964, 'error': 0.000195923556, 'line': 780}
    assert stats['energy'] is stats['mixed_estimators']['total_energy']
    assert set(stats['mixed_estimators']) == {'total_energy', 'kinetic_ti', 'kinetic_kei', 'kinetic_fisq', 'ee_interaction', 'ei_interaction'}
    assert stats['variance']['value'] == pytest.approx(0.055069434742)
    assert stats['time_step']['value'] is not None
    assert stats['target_weight']['value'] is not None
    assert parsed['result']['kind'] == 'dmc_stats'


def test_every_dmc_block_is_kept_not_just_the_last(out_file):
    """Both DMC phases are checkpointed, and a block is where a wandering population shows up."""
    parsed = parse_out(out_file('vmc_dmc'))
    equilibration, stats = parsed['phases'][1], parsed['phases'][2]
    assert (equilibration['nblock'], stats['nblock']) == (2, 20)
    assert [len(phase['blocks']) for phase in (equilibration, stats)] == [2, 20]

    lines = [block['line'] for block in stats['blocks']]
    assert lines == sorted(lines), 'blocks must come back in the order CASINO wrote them'
    assert all(block['best_energy']['value'] is not None for block in stats['blocks'])
    assert all(block['acceptance']['value'] is not None for block in stats['blocks'])


def test_bad_reblock_convergence_is_reported_not_hidden(out_file):
    parsed = parse_out(out_file('vmc_dmc'))
    assert parsed['messages'] == [{'line': 412, 'text': 'Bad reblock convergence - probably not enough data samples.'}]
    assert parsed['phases'][0]['reblock_converged'] is False


def test_reblock_dump_is_read_and_the_best_row_marked(out_file):
    """The quoted error bar is one row of that table; which row, and whether it plateaued, is the point."""
    stats = parse_out(out_file('vmc_dmc'))['phases'][2]
    dump = stats['reblock']
    assert dump['stderr']['value'] == stats['energy']['error']
    assert dump['best_block_length'] == 256
    best = [row for row in dump['rows'] if row.get('best')]
    assert [row['length'] for row in best] == [256]
    assert best[0]['stderr'] == pytest.approx(stats['energy']['error'])
    lengths = [row['length'] for row in dump['rows']]
    assert lengths == [2**n for n in range(len(lengths))], 'the table is one row per block length, doubling'


def test_a_vmc_phase_carries_the_dump_only_when_reblocking_failed(out_file):
    """The asymmetry is CASINO's: `vmc.f90` prints it inside the `derr > 0.1*err` branch.

    So in a VMC phase the dump is a diagnostic that shows up exactly when the error bar is in
    doubt, and its absence is the good case -- while a DMC phase always has one. Anything that
    treats it as an always-present field is wrong, which is how the field list of the examples
    re-run learnt to skip it.
    """
    converged = parse_out(out_file('vmc_single'))['phases'][0]
    assert converged['reblock_converged'] is True
    assert 'reblock' not in converged

    failed = parse_out(out_file('vmc_dmc'))['phases'][0]
    assert failed['reblock_converged'] is False
    assert failed['reblock']['stderr']['value'] == failed['energy_errors']['reblock']['value']


def test_a_running_dmc_run_is_read_from_dmc_status(out_file):
    """`out` carries no DMC energy until the run ends. While it runs, dmc.status is the only source."""
    parsed = parse_out(out_file('dmc_running'))
    assert parsed['complete'] is False
    assert [phase['kind'] for phase in parsed['phases']] == ['vmc', 'dmc_equil', 'dmc_stats']
    stats = parsed['phases'][2]
    assert stats['nblock'] == 9
    assert stats['energy']['value'] is None, 'the mixed estimators are written when the run ends, not before'
    assert 'dmc.status' in stats['energy']['reason']

    current = parsed['dmc_status']
    assert current['path'].endswith('dmc.status')
    assert current['energy'] == {'value': -14.667081101447, 'error': 2.9894476e-05, 'line': 6}
    assert current['data_points']['value'] == 90000
    assert current['units']['value'] == '(au)'
    assert current['reblock']['best_block_length'] == 256
    assert current['reblock_converged'] is True


def test_the_result_of_a_running_dmc_run_is_never_the_vmc_phase(out_file):
    """The VMC phase of a vmc_dmc run is configuration generation: its energy is the trial wave function's."""
    parsed = parse_out(out_file('dmc_running'))
    vmc = parsed['phases'][0]
    assert vmc['energy']['value'] is not None, 'the VMC phase does report an energy -- that is the trap'
    result = parsed['result']
    assert result['source'] == 'dmc.status'
    assert result['energy'] is parsed['dmc_status']['energy']
    assert result['energy']['value'] != vmc['energy']['value']


def test_dmc_status_is_parsed_the_same_way_standalone(out_file):
    """One parser for the file and for the section of `out` it is copied into when the run ends."""
    directory = out_file('dmc_running').parent
    assert parse_dmc_status(directory) == parse_dmc_status(directory / 'dmc.status')
    assert parse_dmc_status(directory) == parse_out(directory)['dmc_status']


def test_statistics_of_a_live_run_carry_the_population_analysis(out_file):
    """popstats : T is what puts this section in the file; without it there is no such block."""
    current = parse_dmc_status(out_file('dmc_running').parent)
    assert current['variance']['value'] == pytest.approx(0.019266923223)
    assert current['target_weight']['value'] == 1024.0
    assert current['average_population']['value'] == pytest.approx(1023.954111111111)
    assert current['time_step']['value'] == pytest.approx(0.02083)


def test_dmc_status_note_says_the_file_is_transient(out_file):
    """A number read from a file that will not exist tomorrow has to say so."""
    note = parse_dmc_status(out_file('dmc_running').parent)['note']
    assert 'deletes' in note and 'block' in note


def test_interrupted_run_invents_no_energy(out_file):
    """envmc rebuilds an energy from the blocks of an interrupted phase. We report what CASINO wrote."""
    parsed = parse_out(out_file('interrupted'))
    assert parsed['complete'] is False
    phase = parsed['phases'][0]
    assert phase['nblock'] == 8
    assert phase['energy']['value'] is None
    assert phase['energy']['reason'] == 'no FINAL RESULT block in this phase'
    assert parsed['result'] == {'value': None, 'reason': 'no phase in this file reports an energy'}
    for key in ('cpu_time', 'real_time', 'ended'):
        assert parsed[key]['value'] is None
        assert parsed[key]['reason']


def test_a_file_with_no_phase_in_it_parses_to_no_phases(tmp_path):
    """A run that errstopped before its first phase, and anything that is not an `out` at all.

    `split_phases` paired each phase with the next one under `strict=True`, and with no phase
    there was still a sentinel to pair it against, so this raised ValueError out of
    `casino_results` instead of answering -- for the very run whose failure has to be read.
    """
    path = tmp_path / 'out'
    path.write_text(' Started 2026/08/25 13:48:02.391\n\n Cannot open gwfn.data\n')
    parsed = parse_out(path)
    assert parsed['phases'] == []
    assert parsed['complete'] is False
    assert parsed['result'] == {'value': None, 'reason': 'no phase in this file reports an energy'}
    assert parsed['started']['value'] is not None  # what the file does say is still read


def test_block_means_are_labelled_derived(out_file):
    """A derived number must say so, and must never sit where a printed one would."""
    parsed = parse_out(out_file('interrupted'))
    phase = parsed['phases'][0]
    assert phase['acceptance']['derived'] == 'mean over 8 blocks'
    assert 'line' not in phase['acceptance']
    assert phase['acceptance']['value'] == pytest.approx(50.0817125)


def test_every_printed_number_carries_its_line(out_file):
    parsed = parse_out(out_file('vmc_dmc'))
    lines = out_file('vmc_dmc').read_text().split('\n')
    energy = parsed['result']['energy']
    assert 'Total energy' in lines[energy['line'] - 1]
    assert str(abs(energy['value']))[:8] in lines[energy['line'] - 1]


def test_directory_and_file_are_both_accepted(out_file):
    path = out_file('vmc_single')
    assert parse_out(path) == parse_out(path.parent)


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        parse_out(tmp_path / 'nowhere')


# --- the field selector ---------------------------------------------------------------


def test_a_phase_kind_means_the_last_phase_of_that_kind(out_file):
    parsed = parse_out(out_file('vmc_opt_emin'))
    values, reasons, problems = select(parsed, ['vmc.acceptance', 'vmc.efficiency', 'vmc.correlation_time', 'vmc.dtvmc', 'cpu_time'])

    assert problems == [] and reasons == {}
    assert values == {
        'vmc.acceptance': 45.419,
        'vmc.efficiency': 94809000.0,
        'vmc.correlation_time': 3.1413,
        'vmc.dtvmc': 0.74042,
        'cpu_time': 11.78,
    }
    # the last vmc phase, which in a vmc_opt run is the post-fit one and not cycle 1
    assert values['vmc.acceptance'] == parsed['phases'][-1]['acceptance']['value']


def test_a_cycle_is_addressed_by_the_number_casino_gave_it(out_file):
    parsed = parse_out(out_file('vmc_opt_emin'))
    values, _, problems = select(parsed, ['opt[3].nparam', 'opt[3].method', 'phases[0].kind', 'phases[-1].kind'])

    assert problems == []
    assert values == {'opt[3].nparam': 11.0, 'opt[3].method': 'emin', 'phases[0].kind': 'vmc', 'phases[-1].kind': 'vmc'}


def test_the_step_asked_for_against_the_step_the_run_used(out_file):
    """The pair that catches an opt_dtvmc scan measuring one point 38 times."""
    values, _, _ = select(parse_out(out_file('vmc_single')), ['keywords.DTVMC', 'vmc.dtvmc'])

    assert values['keywords.DTVMC'] == '1.0000E-02'
    assert values['vmc.dtvmc'] == 9.4391e-02


def test_a_number_casino_did_not_print_is_null_with_its_reason(out_file):
    parsed = parse_out(out_file('interrupted'))
    values, reasons, problems = select(parsed, ['cpu_time', 'ended'])

    assert problems == []
    assert values == {'cpu_time': None, 'ended': None}
    assert 'did not reach the timing report' in reasons['cpu_time']


def test_a_path_that_does_not_exist_is_a_mistake_in_the_question(out_file):
    parsed = parse_out(out_file('vmc_single'))
    values, _, problems = select(parsed, ['vmc.efficency', 'dmc_stats.energy', 'vmc[9].energy', 'nonsense.x', 'vmc.blocks[7]'])

    assert values == {}
    assert 'no efficency under vmc' in problems[0] and 'efficiency' in problems[0]  # what is there instead
    assert 'no dmc_stats phase' in problems[1]
    assert 'numbered' in problems[2]
    assert 'no nonsense under the top level' in problems[3]
    assert 'so there is no vmc.blocks[7]' in problems[4]


def test_the_projection_is_two_orders_of_magnitude_smaller(out_file):
    """The reason it exists: six numbers a point instead of the whole run, 38 times over."""
    parsed = parse_out(out_file('vmc_dmc'))
    values, _, _ = select(parsed, ['dmc_stats.energy', 'dmc_stats.variance', 'dmc_stats.target_weight', 'cpu_time'])

    assert len(json.dumps(values)) < len(json.dumps(parsed)) / 50
