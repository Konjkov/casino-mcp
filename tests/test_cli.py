"""The `casino-mcp` command.

It exists so the control plane can be inspected without the model that drives it: every
subcommand prints JSON to stdout and exits non-zero when the JSON carries an `error`, so a
shell script and the model see the same thing.
"""

import json

import pytest

from casino_mcp import __version__, cli, settings


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured


def test_no_arguments_prints_help(capsys):
    code, captured = run(capsys)
    assert code == 2
    assert 'casino-mcp' in captured.out and 'serve' in captured.out


def test_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(['--version'])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_config_prints_the_installation_the_server_will_use(capsys, monkeypatch, tmp_path, fake_runqmc):
    script = fake_runqmc()
    monkeypatch.setenv('CASINO_HOME', str(tmp_path / 'CASINO'))
    code, captured = run(capsys, 'config')

    data = json.loads(captured.out)
    assert code == 0
    assert data['casino_home'] == str(tmp_path / 'CASINO')
    assert data['runqmc'] == str(script)
    assert data['environment']['CASINO_HOME'] == str(tmp_path / 'CASINO')


def test_config_says_when_nothing_is_set(capsys):
    code, captured = run(capsys, 'config')
    assert code == 0
    assert json.loads(captured.out)['environment'] == dict.fromkeys(json.loads(captured.out)['environment'])
    assert 'no CASINO_* variable is set' in captured.err


def test_config_says_when_runqmc_cannot_be_found(capsys, monkeypatch, tmp_path):
    # both fallbacks have to be empty, or the developer's own installation answers instead
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    monkeypatch.setenv('CASINO_HOME', str(tmp_path / 'nowhere'))
    code, captured = run(capsys, 'config')
    assert code == 0
    assert json.loads(captured.out)['runqmc'] is None
    assert 'runqmc not found' in captured.err


def test_help_documents_every_variable_that_is_read(capsys):
    code, captured = run(capsys)
    assert code == 2
    for name, _ in settings.ENVIRONMENT:
        assert name in captured.out


def test_jobs_on_an_empty_registry(capsys):
    code, captured = run(capsys, 'jobs')
    assert code == 0
    assert json.loads(captured.out) == {'jobs': []}


def test_wait_and_status_take_a_directory(capsys, workdir, fake_runqmc, python_path):
    """The shell loop `while pgrep -x casino` replaced, and no job id to carry around."""
    fake_runqmc(sleep=0.5)
    code, captured = run(capsys, 'run', str(workdir), '--restart')
    assert code == 0

    code, captured = run(capsys, 'wait', str(workdir))
    waited = json.loads(captured.out)
    assert code == 0 and waited['status'] == 'finished' and waited['timed_out'] is False

    code, captured = run(capsys, 'jobs', '-C', str(workdir))
    assert [job['job_id'] for job in json.loads(captured.out)['jobs']] == [waited['job_id']]


def test_waiting_on_a_directory_nothing_ran_in_exits_nonzero(capsys, workdir):
    code, captured = run(capsys, 'wait', str(workdir))
    assert code == 1
    assert 'no job has run in' in json.loads(captured.out)['error']


def test_results_can_be_asked_for_a_few_fields(capsys, workdir, fake_runqmc, python_path, out_file):
    """What a scan reads: the numbers, not the run."""
    fake_runqmc(out_text=(out_file('vmc_single')).read_text())
    run(capsys, 'run', str(workdir), '--restart')
    run(capsys, 'wait', str(workdir))

    code, captured = run(capsys, 'results', str(workdir), '-f', 'vmc.acceptance,vmc.efficiency', '-f', 'keywords.DTVMC')
    answer = json.loads(captured.out)
    assert code == 0
    assert sorted(answer['fields']) == ['keywords.DTVMC', 'vmc.acceptance', 'vmc.efficiency']
    assert 'phases' not in answer

    code, captured = run(capsys, 'results', str(workdir), '-f', 'vmc.efficency')
    assert code == 1
    assert 'no efficency under vmc' in json.loads(captured.out)['problems'][0]


def test_input_reads_a_directory_that_has_never_been_run(capsys, workdir):
    code, captured = run(capsys, 'input', str(workdir))
    assert code == 0
    assert json.loads(captured.out)['keywords']['runtype'] == 'vmc'


def test_status_of_an_unknown_job_exits_nonzero(capsys):
    code, captured = run(capsys, 'status', 'nope')
    assert code == 1
    assert json.loads(captured.out)['error'].startswith('unknown job nope')


def test_results_of_an_unknown_job_exits_nonzero(capsys):
    code, captured = run(capsys, 'results', 'nope')
    assert code == 1
    assert json.loads(captured.out)['error'].startswith('unknown job nope')


def test_parse_of_a_running_dmc_directory_reads_dmc_status_too(capsys, out_file):
    """`parse` is the file-level twin of `results`, and must not disagree with it about the energy."""
    code, captured = run(capsys, 'parse', str(out_file('dmc_running').parent))
    parsed = json.loads(captured.out)
    assert code == 0
    assert parsed['complete'] is False
    assert parsed['result']['source'] == 'dmc.status'
    assert parsed['dmc_status']['energy']['value'] == -14.667081101447


def test_parse_prints_a_structured_out_file(capsys, out_file):
    code, captured = run(capsys, 'parse', str(out_file('vmc_single')))
    parsed = json.loads(captured.out)
    assert code == 0
    assert parsed['result']['energy']['value'] == -2.862098498845


def test_parse_accepts_a_directory(capsys, out_file):
    code, captured = run(capsys, 'parse', str(out_file('vmc_dmc').parent))
    assert code == 0
    assert json.loads(captured.out)['runtype'] == 'vmc_dmc'


def test_parse_of_a_missing_file_is_an_error_not_a_traceback(capsys, tmp_path):
    code, captured = run(capsys, 'parse', str(tmp_path / 'nowhere'))
    assert code == 1
    assert 'cannot read' in json.loads(captured.out)['error']


def test_run_refuses_and_exits_nonzero(capsys, tmp_path):
    code, captured = run(capsys, 'run', str(tmp_path / 'nowhere'))
    assert code == 1
    assert 'not a directory' in json.loads(captured.out)['error']


def test_run_forwards_its_flags(monkeypatch, capsys, workdir):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'start', lambda workdir, **kwargs: seen.update(workdir=workdir, **kwargs) or {'job_id': 'x'})
    code, _ = run(capsys, 'run', str(workdir), '-p', '4', '--version', 'debug', '--restart', '--unlock')

    assert code == 0
    assert seen == {'workdir': str(workdir), 'nproc': 4, 'version': 'debug', 'restart': True, 'resume': False, 'unlock': True}


def test_continue_is_spelled_the_way_runqmc_spells_it(monkeypatch, capsys, workdir):
    """`continue` cannot be a Python identifier; on the command line it can still be the flag."""
    seen = {}
    monkeypatch.setattr(cli.runtime, 'start', lambda workdir, **kwargs: seen.update(workdir=workdir, **kwargs) or {'job_id': 'x'})
    run(capsys, 'run', str(workdir), '--continue')

    assert seen['resume'] is True and seen['restart'] is False


def test_prepare_forwards_its_flags(monkeypatch, capsys, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'prepare', lambda source, dest, **kwargs: seen.update(source=source, dest=dest, **kwargs) or {})
    code, _ = run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--runtype', 'vmc_dmc', '-s', 'dtdmc=0.005', '-s', 'opt_plan=')

    assert code == 0
    assert seen['runtype'] == 'vmc_dmc'
    assert seen['overrides'] == {'dtdmc': '0.005', 'opt_plan': None}
    assert seen['jastrow'] is None, 'a Jastrow is written only when it is asked for'


def test_prepare_takes_the_jastrow_terms_as_a_list(monkeypatch, capsys, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'prepare', lambda source, dest, **kwargs: seen.update(**kwargs) or {})
    run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--jastrow', 'u,chi', '-j', 'n_u=4', '-j', 'cutoff_u=6.5')

    assert seen['jastrow'] == ['u', 'chi']
    assert seen['jastrow_settings'] == {'n_u': 4, 'cutoff_u': 6.5}, 'an order is an integer and a cutoff is not'


def test_a_bare_jastrow_flag_means_all_the_terms(monkeypatch, capsys, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'prepare', lambda source, dest, **kwargs: seen.update(**kwargs) or {})
    run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--jastrow', '--backflow')
    assert seen['jastrow'] == ['u', 'chi', 'f']
    assert seen['backflow'] == ['eta', 'mu', 'phi']


def test_the_two_blocks_are_asked_for_separately(monkeypatch, capsys, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'prepare', lambda source, dest, **kwargs: seen.update(**kwargs) or {})
    run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--backflow', 'eta,mu')
    assert seen['jastrow'] is None and seen['backflow'] == ['eta', 'mu']


def test_a_jastrow_setting_that_is_not_a_number_is_refused(capsys, tmp_path):
    with pytest.raises(SystemExit, match='expects a number'):
        run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--jastrow', '-j', 'n_u=eight')


def test_prepare_takes_the_geminal_channels_as_a_list(monkeypatch, capsys, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'prepare', lambda source, dest, **kwargs: seen.update(**kwargs) or {})
    run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--geminal', 'p:2,d:1', '-g', 'seed=-0.19', '-g', 'anchors=1,2')

    assert seen['geminal'] == ['p:2', 'd:1']
    assert seen['geminal_settings'] == {'seed': -0.19, 'anchors': [1, 2]}, 'a seed is a number and the anchors are a list of orbitals'


def test_a_bare_geminal_flag_is_the_hartree_fock_one(monkeypatch, capsys, tmp_path):
    """No channels is a decision -- Geminal 1 alone -- and not the same as not asking at all."""
    seen = {}
    monkeypatch.setattr(cli.runtime, 'prepare', lambda source, dest, **kwargs: seen.update(**kwargs) or {})
    run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--geminal')
    assert seen['geminal'] == []

    seen.clear()
    run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'))
    assert seen['geminal'] is None, 'a parameters.casl is written only when it is asked for'


def test_a_geminal_setting_that_is_not_a_number_is_refused(capsys, tmp_path):
    with pytest.raises(SystemExit, match='expects a number'):
        run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--geminal', '-g', 'seed=low')


def test_anchors_that_are_not_orbital_numbers_are_refused(capsys, tmp_path):
    with pytest.raises(SystemExit, match='expects orbital numbers'):
        run(capsys, 'prepare', str(tmp_path / 'a'), str(tmp_path / 'b'), '--geminal', '-g', 'anchors=first')


def test_stop_forwards_its_timeout(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'stop', lambda job_id, **kwargs: seen.update(job_id=job_id, **kwargs) or {})
    run(capsys, 'stop', 'j', '--timeout', '3')
    assert seen == {'job_id': 'j', 'timeout': 3.0}


def test_stop_without_a_timeout_uses_the_default(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli.runtime, 'stop', lambda job_id, **kwargs: seen.update(job_id=job_id, **kwargs) or {})
    run(capsys, 'stop', 'j')
    assert seen == {'job_id': 'j', 'timeout': settings.STOP_TIMEOUT}


def test_serve_is_wired_to_the_mcp_server(monkeypatch, capsys):
    """The one subcommand that must not print to stdout: stdout is the JSON-RPC stream."""
    import casino_mcp.server

    started = []
    monkeypatch.setattr(casino_mcp.server.server, 'run', lambda **kwargs: started.append(kwargs))
    code, captured = run(capsys, 'serve')

    assert code == 0
    assert started == [{'transport': 'stdio'}]
    assert captured.out == ''


def test_settings_are_read_at_call_time_not_at_import(capsys, monkeypatch, tmp_path):
    """No cached config object, so a variable set after import still takes effect."""
    monkeypatch.setenv('CASINO_HOME', str(tmp_path / 'later'))
    _, captured = run(capsys, 'config')
    assert json.loads(captured.out)['casino_home'] == str(tmp_path / 'later')
