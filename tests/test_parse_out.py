"""What `parse_out` promises, against five real CASINO `out` files.

The values below were read out of the fixtures by hand. That is the point: a test that
recomputes them with the parser would only assert that the parser agrees with itself. The
line numbers are asserted too -- they are how a claim made from a parsed number is checked
against the file it came from, so a shifted line is a real defect, not a cosmetic one.

The heavier oracle lives in tests/integration/test_examples_envmc.py: 526 example files
against CASINO's own `envmc`. This suite is the part that runs anywhere, in a second.
"""

import pytest

from casino_mcp.parse_out import parse_out

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
    assert result['energy'] == {'value': -2.861829862553, 'error': 0.000659077167, 'line': 237}
    assert parsed['cpu_time'] == {'value': 9.47, 'line': 245}


def test_single_block_variance_error_is_derived_and_labelled(out_file):
    """CASINO omits the sample-variance error for a one-block run; it is then the block's own."""
    parsed = parse_out(out_file('vmc_single'))
    phase = parsed['phases'][0]
    assert phase['nblock'] == 1
    variance = phase['variance']
    assert variance['value'] == 0.574561788453
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
    assert parsed['result']['energy']['value'] == -0.500282762241


def test_varmin_reports_a_variance_and_no_energy(out_file):
    """Not a gap in the parser: varmin's target is the variance, the energy is the next VMC phase."""
    parsed = parse_out(out_file('vmc_opt_varmin'))
    opt = parsed['phases'][1]
    assert opt['method']['value'] == 'varmin'
    assert opt['variance'] == {'value': 0.0012217187, 'line': 486}
    assert opt['energy']['value'] is None
    assert opt['energy']['reason'] == 'optimization phase reports no final energy'


def test_emin_reports_both(out_file):
    """An emin run opens with one varmin cycle; only the emin phases carry an energy."""
    parsed = parse_out(out_file('vmc_opt_emin'))
    methods = [phase['method']['value'] for phase in parsed['phases'] if phase['kind'] == 'opt']
    assert methods == ['varmin', 'emin', 'emin', 'emin']
    emin = parsed['phases'][3]
    assert emin['method']['value'] == 'emin'
    assert emin['energy']['value'] == pytest.approx(-0.5000971413222693)
    assert emin['energy']['error'] is not None
    assert emin['variance']['value'] is not None


def test_dmc_phases_and_mixed_estimators(out_file):
    parsed = parse_out(out_file('vmc_dmc'))
    assert [phase['kind'] for phase in parsed['phases']] == ['vmc', 'dmc_equil', 'dmc_stats']
    stats = parsed['phases'][2]
    assert stats['energy'] == {'value': -2753.72712628829, 'error': 0.010650491074, 'line': 502}
    assert stats['energy'] is stats['mixed_estimators']['total_energy']
    assert set(stats['mixed_estimators']) == {'total_energy', 'kinetic_ti', 'kinetic_kei', 'kinetic_fisq', 'ee_interaction', 'ei_interaction'}
    assert stats['variance']['value'] == pytest.approx(141.302116774828)
    assert stats['time_step']['value'] is not None
    assert stats['target_weight']['value'] is not None
    assert parsed['result']['kind'] == 'dmc_stats'


def test_bad_reblock_convergence_is_reported_not_hidden(out_file):
    parsed = parse_out(out_file('vmc_dmc'))
    assert parsed['messages'] == [{'line': 412, 'text': 'Bad reblock convergence - probably not enough data samples.'}]
    assert parsed['phases'][0]['reblock_converged'] is False


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
