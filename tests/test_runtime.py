"""Guardrails, command building, and one real launcher round trip against a fake runqmc.

The end-to-end tests here spawn the actual launcher process and signal the actual process
group. They need no CASINO, because what they exercise is ours: the process group, the exit
code that outlives the caller, the log file, and the lock that must be cleared after a stop.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import EXAMPLES

from casino_mcp import input_file, jobs, runtime, settings

# The three ways a CASINO run can end, as `out` records them. Only the markers matter here:
# `Started` opens every run in the file, and the other two lines say how the last one ended.
STARTED = ' Started 2026/08/25 13:48:02.391\n\n Running in parallel using 4 MPI processes.\n'
COMPLETED = STARTED + ' Total CASINO CPU time  : : :       21.3400 s\n Ends 2026/08/25 13:48:23.731\n'
INTERRUPTED = STARTED + ' VMC #  1\n Acceptance ratio         (%)  =  50.9\n'
TIME_LIMITED = STARTED + ' CONTINUATION INFO:\n  Suggested action: continue run directly\n  Set NEWRUN : F\n'


def wait_for(predicate, timeout=30.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f'timed out after {timeout}s')


# --- finding runqmc ------------------------------------------------------------------


def test_explicit_runqmc_wins(fake_runqmc):
    script = fake_runqmc()
    assert runtime.find_runqmc() == str(script)


def test_explicit_runqmc_that_is_not_executable_is_not_silently_replaced(monkeypatch, tmp_path):
    """Falling back to PATH here would run a different binary than $CASINO_RUNQMC names."""
    dud = tmp_path / 'runqmc'
    dud.write_text('#!/bin/sh\n')  # not chmod +x
    monkeypatch.setenv('CASINO_RUNQMC', str(dud))
    assert runtime.find_runqmc() is None


def test_fallback_to_casino_home(tmp_path, monkeypatch, fake_runqmc):
    script = fake_runqmc()
    home = tmp_path / 'CASINO'
    (home / 'bin_qmc').mkdir(parents=True)
    script.replace(home / 'bin_qmc' / 'runqmc')
    monkeypatch.delenv('CASINO_RUNQMC')
    monkeypatch.setenv('CASINO_HOME', str(home))
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    assert runtime.find_runqmc() == str(home / 'bin_qmc' / 'runqmc')


def test_missing_runqmc_says_where_it_looked(workdir, tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    monkeypatch.setenv('CASINO_HOME', str(tmp_path / 'CASINO'))
    error = runtime.start(str(workdir))['error']
    assert 'runqmc not found' in error and str(tmp_path / 'CASINO' / 'bin_qmc') in error


# --- the command ---------------------------------------------------------------------


def test_command_carries_nproc_and_omits_the_default_version():
    assert runtime.build_command('/bin/runqmc', 4, 'opt', unlock=False) == ['/bin/runqmc', '-p', '4']


def test_command_names_a_non_default_version_and_unlock():
    command = runtime.build_command('/bin/runqmc', 2, 'debug', unlock=True)
    assert command == ['/bin/runqmc', '-p', '2', '--version=debug', '--unlock']


def test_resume_is_runqmcs_own_continuation():
    """Continuing is runqmc's job -- ours is to ask for it and get out of the way."""
    assert runtime.build_command('/bin/runqmc', 1, 'opt', unlock=False, resume=True) == ['/bin/runqmc', '-p', '1', '--continue']


# --- refusals ------------------------------------------------------------------------


def test_refuses_a_directory_that_is_not_one(tmp_path):
    assert 'not a directory' in runtime.start(str(tmp_path / 'nowhere'))['error']


def test_refuses_a_directory_without_an_input_file(tmp_path):
    (tmp_path / 'empty').mkdir()
    assert 'no CASINO `input` file' in runtime.start(str(tmp_path / 'empty'))['error']


def test_refuses_an_existing_out_and_says_how_to_proceed(workdir):
    (workdir / 'out').write_text('an earlier run\n')
    error = runtime.start(str(workdir))['error']
    assert 'appends' in error
    assert 'restart=true' in error and 'resume=true' in error


def test_refuses_a_locked_directory(workdir):
    (workdir / '.runqmc.lock').touch()
    error = runtime.start(str(workdir))['error']
    assert '.runqmc.lock' in error and 'unlock=true' in error


def test_refuses_committed_reference_data_harder(workdir):
    """The real risk is destroying an `out` that other work is validated against."""
    if not subprocess.run(['git', 'init', '-q', str(workdir)], check=False).returncode == 0:
        pytest.skip('git not available')
    (workdir / 'out').write_text('reference data\n')
    subprocess.run(['git', '-C', str(workdir), 'add', 'out'], check=True)
    subprocess.run(
        ['git', '-C', str(workdir), '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'reference'],
        check=True,
    )

    error = runtime.start(str(workdir))['error']
    assert 'committed reference data' in error
    assert 'Copy the directory' in error


def test_forbidden_directory_cannot_be_overridden(workdir, monkeypatch, fake_runqmc):
    """`restart` and `unlock` unlock the other two guards. This one has no key at all."""
    fake_runqmc()
    monkeypatch.setenv('CASINO_MCP_FORBID', str(workdir.parent))

    error = runtime.start(str(workdir), restart=True, unlock=True)['error']
    assert 'CASINO_MCP_FORBID' in error and 'No override exists' in error


def test_restart_and_resume_are_opposites(workdir, fake_runqmc):
    fake_runqmc()
    (workdir / 'out').write_text('an earlier run\n')
    assert 'opposites' in runtime.start(str(workdir), restart=True, resume=True)['error']


def test_resume_needs_the_output_it_would_continue_from(workdir, fake_runqmc):
    fake_runqmc()
    error = runtime.start(str(workdir), resume=True)['error']
    assert 'no `out`' in error and 'continuation info' in error


def test_resume_starts_on_an_existing_out_and_keeps_it(workdir, fake_runqmc, python_path):
    fake_runqmc(sleep=0.5)
    (workdir / 'out').write_text(TIME_LIMITED)
    (workdir / 'config.in').write_text('configurations\n')

    started = runtime.start(str(workdir), resume=True)
    assert started['command'].endswith('--continue')
    assert started['resume'] == 'continue'
    assert started['removed'] == []
    assert (workdir / 'config.in').is_file()  # the very thing being continued from


def test_resume_after_a_halt_is_a_plain_runqmc_over_the_updated_input(workdir, fake_runqmc, python_path):
    """A killed run has no continuation info; what continues it is the input haltqmc rewrote."""
    fake_runqmc(sleep=0.5)
    (workdir / 'out').write_text(INTERRUPTED)

    started = runtime.start(str(workdir), resume=True)
    assert '--continue' not in started['command']
    assert started['resume'] == 'halted'
    assert 'haltqmc -u' in started['note']


def test_resume_refuses_a_run_that_reached_its_own_end(workdir, fake_runqmc):
    fake_runqmc()
    (workdir / 'out').write_text(COMPLETED)

    error = runtime.start(str(workdir), resume=True)['error']
    assert 'nothing to continue' in error and 'restart=true' in error


def test_only_the_last_run_in_out_decides_how_to_continue(workdir, fake_runqmc, python_path):
    """Continuing appends, so a directory that was continued once holds several runs."""
    fake_runqmc(sleep=0.5)
    (workdir / 'out').write_text(COMPLETED + INTERRUPTED)

    assert runtime.resume_mode(workdir / 'out') == 'halted'
    assert '--continue' not in runtime.start(str(workdir), resume=True)['command']


def test_restart_deletes_what_the_earlier_run_left_and_nothing_else(workdir, fake_runqmc, python_path):
    fake_runqmc()
    debris = (
        'out',
        'out_part.1',
        '.out_proc0',
        'vmc.hist',
        'dmc.hist',
        'dmc.status',  # a killed DMC run leaves one; parse_out would read it as current
        'vmc.hist.2',
        'config.in',
        'config.out_fixed',
        'correlation.out.3',
        'parameters.4.casl',
    )
    inputs = ('input', 'gwfn.data', 'correlation.data', 'parameters.casl')
    for name in debris + inputs:
        (workdir / name).write_text(name)
    (workdir / 'saved_part_1').mkdir()
    (workdir / 'saved_part_1' / 'input_orig').write_text('an earlier input')

    started = runtime.start(str(workdir), restart=True)

    assert 'job_id' in started
    assert sorted(started['removed']) == sorted(debris + ('saved_part_1',))
    assert not (workdir / 'saved_part_1').exists()
    assert all((workdir / name).is_file() for name in inputs)


def test_restart_refuses_an_input_that_is_set_up_to_continue(workdir, fake_runqmc):
    """CASINO wants the config.in that restarting deletes, so it would fail on NEWRUN : F."""
    fake_runqmc()
    (workdir / 'input').write_text('runtype : vmc\nnewrun  : F   #*! New run or continue old\n')
    (workdir / 'out').write_text(INTERRUPTED)

    error = runtime.start(str(workdir), restart=True)['error']
    assert 'NEWRUN : F' in error and 'resume=true' in error
    assert (workdir / 'out').is_file()  # refused before anything was deleted


def test_a_newrun_that_is_true_restarts_as_usual(workdir, fake_runqmc, python_path):
    fake_runqmc()
    (workdir / 'input').write_text('runtype : vmc\nnewrun  : T   #*! New run or continue old\n')
    (workdir / 'out').write_text(INTERRUPTED)

    assert runtime.start(str(workdir), restart=True)['removed'] == ['out']


def test_a_run_that_cannot_start_deletes_nothing(workdir, monkeypatch):
    """The order matters: an emptied directory plus a refusal is the worst of both."""
    monkeypatch.setenv('CASINO_RUNQMC', str(workdir / 'no-such-runqmc'))
    (workdir / 'out').write_text('an earlier run\n')

    assert 'runqmc not found' in runtime.start(str(workdir), restart=True)['error']
    assert (workdir / 'out').is_file()


def test_unlock_unlocks_a_stale_lock(workdir, fake_runqmc, python_path):
    fake_runqmc()
    (workdir / '.runqmc.lock').touch()
    started = runtime.start(str(workdir), unlock=True)
    assert 'job_id' in started
    assert started['command'].endswith('--unlock')  # runqmc clears its own lock, we do not


def test_refuses_a_nonsense_nproc(workdir, fake_runqmc):
    fake_runqmc()
    assert 'nproc must be at least 1' in runtime.start(str(workdir), nproc=0)['error']


def test_a_refusal_creates_no_job_record(workdir):
    (workdir / 'out').write_text('an earlier run\n')
    runtime.start(str(workdir))
    assert jobs.JobStore().index() == {}


# --- a real launcher round trip ------------------------------------------------------


def test_run_to_completion(workdir, tmp_path, fake_runqmc, python_path):
    fake_runqmc(exit_code=0, out_text='FINAL RESULT: fake\n')
    store = jobs.JobStore()

    started = runtime.start(str(workdir), nproc=2, store=store)
    assert started['command'].endswith('-p 2')
    assert started['job_id'] in store.index()

    state = wait_for(lambda: (s := runtime.status(started['job_id'], store))['status'] != 'running' and s)
    assert state['status'] == 'finished'
    assert state['exit_code'] == 0
    assert state['runtime'] >= 0
    assert (workdir / 'out').read_text() == 'FINAL RESULT: fake\n'
    # runqmc's own chatter goes to the job directory, never into the MCP stdio stream
    assert (tmp_path / 'state' / 'casino-mcp' / 'jobs' / started['job_id'] / 'runqmc.log').is_file()


def test_a_failing_run_is_failed_not_finished(workdir, fake_runqmc, python_path):
    fake_runqmc(exit_code=3)
    started = runtime.start(str(workdir))

    state = wait_for(lambda: (s := runtime.status(started['job_id']))['status'] != 'running' and s)
    assert state['status'] == 'failed' and state['exit_code'] == 3


def test_stop_kills_the_tree_and_clears_the_lock(workdir, fake_runqmc, python_path):
    fake_runqmc(sleep=300)
    started = runtime.start(str(workdir))
    wait_for(lambda: (workdir / '.runqmc.lock').exists())

    stopped = runtime.stop(started['job_id'], timeout=10.0)
    assert stopped['status'] == 'stopped'
    assert not (workdir / '.runqmc.lock').exists()
    assert not jobs.proc_alive(started['pid'], None)


def test_stop_hands_the_directory_to_haltqmc(workdir, fake_runqmc, fake_haltqmc, python_path):
    """Tidying up after a killed run is haltqmc's job, and -u is what makes it continuable."""
    fake_runqmc(sleep=300)
    fake_haltqmc()
    started = runtime.start(str(workdir))
    wait_for(lambda: (workdir / '.runqmc.lock').exists())
    (workdir / 'config.out').write_text('configurations\n')

    halt = runtime.stop(started['job_id'], timeout=10.0)['halt']
    assert halt['exit_code'] == 0 and halt['updated_input'] is True
    assert (workdir / 'haltqmc.args').read_text() == '-f -u'
    # the input as it was before haltqmc rewrote it, kept in the registry and not in the calculation
    saved = Path(halt['input_saved'])
    assert saved.parent == jobs.jobs_dir() / started['job_id']
    assert saved.read_text() == (workdir / 'input').read_text()
    assert 'haltqmc_update_input' in (workdir / 'haltqmc.helper').read_text()
    assert (workdir / 'config.in').read_text() == 'configurations\n'  # what a resume continues from
    assert not (workdir / 'config.out').exists()


def test_stop_without_the_update_helper_says_the_run_cannot_be_continued(workdir, fake_runqmc, fake_haltqmc, python_path):
    """haltqmc errstops on -u without its helper, so the flag is not passed and that is said."""
    fake_runqmc(sleep=300)
    fake_haltqmc(helper=False)
    started = runtime.start(str(workdir))
    wait_for(lambda: (workdir / '.runqmc.lock').exists())

    halt = runtime.stop(started['job_id'], timeout=10.0)['halt']
    assert (workdir / 'haltqmc.args').read_text() == '-f'
    assert halt['updated_input'] is False
    assert 'not on PATH' in halt['note'] and 'restarted but not continued' in halt['note']


def test_a_failing_haltqmc_is_reported_and_the_lock_still_goes(workdir, fake_runqmc, fake_haltqmc, python_path):
    fake_runqmc(sleep=300)
    fake_haltqmc(exit_code=1)
    started = runtime.start(str(workdir))
    wait_for(lambda: (workdir / '.runqmc.lock').exists())
    (workdir / '.runqmc.lock').touch()

    halt = runtime.stop(started['job_id'], timeout=10.0)['halt']
    assert halt['exit_code'] == 1
    assert 'haltqmc exited nonzero' in halt['error']
    assert not (workdir / '.runqmc.lock').exists()


def test_stop_without_haltqmc_says_what_was_not_done(workdir, fake_runqmc, python_path):
    """No haltqmc: the job still stops, but the directory is left as the killed run left it."""
    fake_runqmc(sleep=300)
    started = runtime.start(str(workdir))
    wait_for(lambda: (workdir / '.runqmc.lock').exists())

    stopped = runtime.stop(started['job_id'], timeout=10.0)
    assert stopped['status'] == 'stopped'
    assert 'haltqmc not found' in stopped['halt']['error']
    assert 'by hand' in stopped['halt']['note']
    assert not (workdir / '.runqmc.lock').exists()  # the one thing that is cleared regardless


def test_casino_processes_are_the_ones_in_this_jobs_group(tmp_path):
    """What haltqmc -k pkills over the whole account, narrowed to one job's process group."""
    binary = tmp_path / 'casino'  # a process is named after the file, so this one is `casino`
    shutil.copy(shutil.which('sleep') or '/bin/sleep', binary)
    process = subprocess.Popen([str(binary), '30'], start_new_session=True)
    try:
        assert runtime.casino_processes(process.pid) == [process.pid]
        assert runtime.casino_processes(os.getpid()) == []
    finally:
        process.kill()
        process.wait()


def test_stopping_a_finished_job_says_so_instead_of_signalling(workdir, fake_runqmc, python_path):
    fake_runqmc()
    started = runtime.start(str(workdir))
    wait_for(lambda: runtime.status(started['job_id'])['status'] != 'running')

    stopped = runtime.stop(started['job_id'])
    assert stopped['status'] == 'finished'
    assert stopped['note'] == 'not running, nothing to stop'


def test_unknown_job_ids_are_errors_not_exceptions():
    assert runtime.status('nope') == {'error': 'unknown job nope'}
    assert runtime.stop('nope') == {'error': 'unknown job nope'}


def test_listing_is_newest_first_and_limited(workdir, fake_runqmc, python_path):
    fake_runqmc()
    ids = [runtime.start(str(workdir), restart=True)['job_id'] for _ in range(3)]

    listed = runtime.listing(limit=2)['jobs']
    assert [job['job_id'] for job in listed] == sorted(ids, reverse=True)[:2]


# --- results -------------------------------------------------------------------------


def dmc_job(workdir, fixture):
    """A finished job record over a copy of a fixture calculation. No launcher, no CASINO."""
    for source in fixture.iterdir():
        shutil.copy2(source, workdir / source.name)
    store = jobs.JobStore()
    job_id, job_dir, meta = jobs.create(['runqmc', '-p', '4'], workdir, 4, 'opt')
    meta = jobs.record_pid(job_dir, meta, os.getpid())
    store.add(meta)
    jobs.write_json(job_dir / 'status.json', {'exit_code': 0, 'finished': jobs.now(), 'finished_epoch': time.time()})
    return job_id, store


def test_results_of_an_unknown_job():
    assert 'unknown job' in runtime.results('nope')['error']


def test_results_before_casino_has_written_anything(workdir, out_file):
    """A job whose runqmc has not produced an `out` yet reports that, and says where its own log is."""
    job_id, store = dmc_job(workdir, out_file('dmc_running').parent)
    (workdir / 'out').unlink()
    report = runtime.results(job_id, store)
    assert 'no `out`' in report['error']
    assert report['job_id'] == job_id
    assert 'job directory' in report['note']


def test_results_of_a_running_dmc_job_come_from_dmc_status(workdir, out_file):
    """The point of the tool: a DMC run that has not ended still has an answer, and it is not the VMC one."""
    job_id, store = dmc_job(workdir, out_file('dmc_running').parent)
    report = runtime.results(job_id, store)

    assert report['job_id'] == job_id
    assert report['workdir'] == str(workdir)
    assert report['complete'] is False
    assert report['result']['source'] == 'dmc.status'
    assert report['result']['energy']['value'] == -14.667081101447
    assert report['dmc_status']['path'] == str(workdir / 'dmc.status')
    assert report['result']['energy']['value'] != report['phases'][0]['energy']['value']


def test_results_without_a_dmc_status_read_out_alone(workdir, out_file):
    """The same job once CASINO has deleted the file: the numbers are then in `out` itself."""
    job_id, store = dmc_job(workdir, out_file('dmc_running').parent)
    (workdir / 'dmc.status').unlink()
    report = runtime.results(job_id, store)

    assert 'dmc_status' not in report
    assert report['result']['value'] is None
    assert 'no DMC energy exists yet' in report['result']['reason']


def test_results_carry_the_job_state_so_the_numbers_can_be_judged(workdir, out_file):
    job_id, store = dmc_job(workdir, out_file('dmc_running').parent)
    report = runtime.results(job_id, store)
    assert report['status'] == 'finished'
    assert report['nproc'] == 4
    assert report['exit_code'] == 0
    assert report['path'] == str(workdir / 'out')  # every number is checkable against a file


# --- preparing the next calculation ---------------------------------------------------


@pytest.fixture
def calculation(tmp_path, out_file):
    """A directory that holds a real vmc_dmc calculation, products and all."""
    path = tmp_path / 'source'
    path.mkdir()
    for source in out_file('dmc_running').parent.iterdir():
        shutil.copy2(source, path / source.name)
    (path / 'stowfn.data').write_text('orbitals\n')
    (path / 'correlation.data').write_text('a Jastrow factor\n')
    (path / 'parameters.casl').write_text('GEMINAL:\n')
    (path / 'config.out').write_text('where the run left off\n')
    return path


def test_prepare_copies_the_inputs_and_none_of_the_products(calculation, tmp_path):
    prepared = runtime.prepare(str(calculation), str(tmp_path / 'next'))
    assert set(prepared['copied']) == {'input', 'stowfn.data', 'correlation.data', 'parameters.casl'}
    for product in ('out', 'dmc.status', 'config.out'):
        assert not (tmp_path / 'next' / product).exists(), f'{product} is what a run left, not what it was given'


def test_prepare_carries_the_configurations_a_dmc_only_runtype_starts_from(calculation, tmp_path):
    """A finished run leaves them as `config.out`; `config.in` is the name they are read under."""
    prepared = runtime.prepare(str(calculation), str(tmp_path / 'dmc'), runtype='dmc_dmc')
    assert 'config.out -> config.in' in prepared['copied']
    assert (tmp_path / 'dmc' / 'config.in').read_text() == (calculation / 'config.out').read_text()
    assert not (tmp_path / 'dmc' / 'config.out').exists(), 'the new run has not left off anywhere yet'


def test_prepare_carries_no_configurations_into_a_run_that_starts_fresh(calculation, tmp_path):
    """Into a vmc_dmc directory a stray config.out would continue the old run instead of starting."""
    prepared = runtime.prepare(str(calculation), str(tmp_path / 'again'))
    assert not any('config' in name for name in prepared['copied'])
    assert not (tmp_path / 'again' / 'config.in').exists()


def test_prepare_leaves_the_calculation_it_copied_from_alone(calculation, tmp_path):
    before = (calculation / 'input').read_text()
    runtime.prepare(str(calculation), str(tmp_path / 'next'), overrides={'dtdmc': '0.005'})
    assert (calculation / 'input').read_text() == before


def test_prepare_fills_in_what_a_new_runtype_needs(calculation, tmp_path):
    """Switching runtype is one keyword in the file and a dozen in what CASINO then demands."""
    prepared = runtime.prepare(str(calculation), str(tmp_path / 'opt'), runtype='vmc_opt', overrides={'opt_backflow': 'F'})
    written = input_file.read(tmp_path / 'opt' / 'input')['keywords']
    assert prepared['runtype'] == 'vmc_opt'
    assert written['opt_method'] == input_file.RECIPES['vmc_opt']['opt_method']
    assert written['neu'] == '2', 'what the source already said survives'
    assert written['dtdmc'] == '0.02083', 'and so does what no one asked to change'
    assert any('no DMC phase' in warning for warning in prepared['warnings'])


def test_prepare_refuses_before_writing_anything(calculation, tmp_path):
    """A refusal must leave no half-made directory behind."""
    prepared = runtime.prepare(str(calculation), str(tmp_path / 'next'), overrides={'vmc_nconfig_write': '16'})
    assert 'does not describe a run CASINO can do' in prepared['error']
    assert any('below dmc_target_weight' in problem for problem in prepared['problems'])
    assert not (tmp_path / 'next').exists()


def test_prepare_refuses_a_directory_that_already_holds_a_calculation(calculation, tmp_path):
    (tmp_path / 'next').mkdir()
    (tmp_path / 'next' / 'input').write_text('runtype : vmc\n')
    error = runtime.prepare(str(calculation), str(tmp_path / 'next'))['error']
    assert 'already exists and is not empty' in error


def test_prepare_refuses_to_edit_the_source_in_place(calculation):
    assert 'same directory' in runtime.prepare(str(calculation), str(calculation))['error']


def test_prepare_names_what_only_the_caller_can_supply(tmp_path):
    source = tmp_path / 'bare'
    source.mkdir()
    (source / 'input').write_text('runtype : vmc\n')
    prepared = runtime.prepare(str(source), str(tmp_path / 'next'), runtype='vmc_dmc')
    assert 'neu, ned, atom_basis_type' in prepared['error']
    assert prepared['fix'] == 'pass them in overrides'


def test_prepare_refuses_an_unknown_runtype(calculation, tmp_path):
    error = runtime.prepare(str(calculation), str(tmp_path / 'next'), runtype='dmc_md')['error']
    assert 'no recipe for runtype dmc_md' in error and 'vmc_dmc' in error


def test_prepare_says_which_keywords_it_changed(calculation, tmp_path):
    prepared = runtime.prepare(str(calculation), str(tmp_path / 'next'), overrides={'dtdmc': '0.005', 'dmc_target_weight': '1024.0'})
    assert prepared['changed'] == {'dtdmc': '0.005'}, 'a value that was already what was asked for did not change'


# --- the blank Jastrow that goes with it ----------------------------------------------

# As much of a gwfn.data as the geometry needs: a molecule of two elements, one of them twice.
ORBITALS = """TITLE
 water

GEOMETRY
--------
Number of atoms:
         3
Atomic numbers for each atom:
         8         1         1
Valence charges for each atom:
 6.0000000000000E+00 1.0000000000000E+00 1.0000000000000E+00

BASIS SET
"""


@pytest.fixture
def orbitals_only(tmp_path):
    """A directory as an orbital code leaves it: a wave function, an input, and no Jastrow."""
    path = tmp_path / 'hf'
    path.mkdir()
    (path / 'gwfn.data').write_text(ORBITALS)
    (path / 'input').write_text(input_file.build('vmc', {'neu': '5', 'ned': '5', 'atom_basis_type': 'gaussian'}))
    return path


def test_prepare_writes_the_jastrow_a_calculation_with_none_needs(orbitals_only, tmp_path):
    """The first calculation of a chain: `use_jastrow : T` and nothing for it to read."""
    prepared = runtime.prepare(
        str(orbitals_only), str(tmp_path / 'opt'), runtype='vmc_opt', overrides={'vmc_nconfig_write': '10000'}, jastrow=['u', 'chi', 'f']
    )
    assert 'error' not in prepared, prepared
    written = (tmp_path / 'opt' / 'correlation.data').read_text()
    assert ' START JASTROW' in written and ' END JASTROW' in written
    assert 'correlation.data (written blank)' in prepared['copied']
    assert prepared['correlation_data']['sets'] == [{'atomic_number': 8, 'atoms': [1]}, {'atomic_number': 1, 'atoms': [2, 3]}]


def test_the_input_is_not_refused_for_the_jastrow_that_is_about_to_be_written(orbitals_only, tmp_path):
    """`use_jastrow : T` with no correlation.data is a refusal -- unless it comes with one."""
    assert 'error' in runtime.prepare(str(orbitals_only), str(tmp_path / 'a'), runtype='vmc')
    assert 'error' not in runtime.prepare(str(orbitals_only), str(tmp_path / 'b'), runtype='vmc', jastrow=['u'])


def test_prepare_will_not_blank_a_jastrow_that_already_exists(orbitals_only, tmp_path):
    (orbitals_only / 'correlation.data').write_text('an optimized Jastrow factor\n')
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'opt'), runtype='vmc_opt', jastrow=['u'])
    assert 'already has a correlation.data' in prepared['error']
    assert not (tmp_path / 'opt').exists()


def test_a_jastrow_the_input_would_not_read_is_refused(orbitals_only, tmp_path):
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'opt'), overrides={'use_jastrow': 'F'}, jastrow=['u'])
    assert any('use_jastrow' in problem for problem in prepared['problems'])
    assert not (tmp_path / 'opt').exists()


def test_a_jastrow_that_backflow_would_leave_half_written_is_refused(orbitals_only, tmp_path):
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'bf'), overrides={'backflow': 'T'}, jastrow=['u', 'chi', 'f'])
    assert any('BACKFLOW block' in problem for problem in prepared['problems'])


def test_prepare_writes_both_blocks_into_the_one_file(orbitals_only, tmp_path):
    prepared = runtime.prepare(
        str(orbitals_only),
        str(tmp_path / 'bf'),
        overrides={'backflow': 'T'},
        jastrow=['u', 'chi', 'f'],
        backflow=['eta', 'mu', 'phi'],
    )
    assert 'error' not in prepared, prepared
    written = (tmp_path / 'bf' / 'correlation.data').read_text()
    assert ' START JASTROW' in written and ' START BACKFLOW' in written
    assert prepared['correlation_data']['backflow'] == ['eta', 'mu', 'phi']


def test_a_backflow_block_the_input_would_not_read_is_refused(orbitals_only, tmp_path):
    """`backflow : F` and a BACKFLOW block is the mirror of the case above, and as wrong."""
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'bf'), jastrow=['u'], backflow=['eta'])
    assert any('backflow : T' in problem for problem in prepared['problems']), prepared
    assert not (tmp_path / 'bf').exists()


def test_the_backflow_cusp_type_is_derived_from_the_directory(orbitals_only, tmp_path):
    """A pseudopotential in the directory is what makes a set's nucleus not bare."""
    (orbitals_only / 'o_pp.data').write_text('a pseudopotential\nAtomic number and pseudo-charge\n8 6.0\n')
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'bf'), overrides={'backflow': 'T'}, jastrow=['u'], backflow=['mu'])
    assert 'error' not in prepared, prepared
    assert prepared['correlation_data']['pseudo'] == [8]
    written = (tmp_path / 'bf' / 'correlation.data').read_text().splitlines()
    types = [line.strip() for index, line in enumerate(written) if 'cusp conditions' in written[index - 1]]
    assert types == ['0', '1'], 'oxygen is behind a pseudopotential and the hydrogens are not'


def test_a_basis_whose_geometry_is_not_readable_says_so(orbitals_only, tmp_path):
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'pw'), overrides={'atom_basis_type': 'plane-wave'}, jastrow=['u'])
    assert any('pwfn.data' in problem for problem in prepared['problems'])


def test_the_jastrow_settings_reach_the_file(orbitals_only, tmp_path):
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'opt'), jastrow=['u'], jastrow_settings={'n_u': 4, 'cutoff_u': 6.5})
    assert 'error' not in prepared, prepared
    written = (tmp_path / 'opt' / 'correlation.data').read_text()
    assert ' Expansion order N_u\n   4\n' in written
    assert '   6.5' in written


# --- the geminal -----------------------------------------------------------------------

# A calculation with a real gwfn.data behind it: the levels a channel names are read off the
# orbital coefficients, and the `orbitals_only` fixture above carries a header and no orbitals.
GAUSSIAN = EXAMPLES / 'gwfn' / 'Be' / 'MP2-CASSCF(2.4)' / 'cc-pVQZ' / 'CBCS' / 'Jastrow_emin'


@pytest.fixture
def beryllium(tmp_path):
    """The Be example stripped to what an orbital code leaves, with a geminal input over it."""
    path = tmp_path / 'be'
    path.mkdir()
    shutil.copy2(GAUSSIAN / 'gwfn.data', path / 'gwfn.data')
    values = {'neu': '2', 'ned': '2', 'atom_basis_type': 'gaussian', 'psi_s': 'geminal', 'use_jastrow': 'F'}
    (path / 'input').write_text(input_file.build('vmc', values))
    return path


def test_prepare_writes_the_hartree_fock_geminal_with_no_orbital_file_of_its_own(orbitals_only, tmp_path):
    """No channels, no levels to read: the occupied diagonal is what `neu` and `ned` say it is."""
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'gem'), overrides={'psi_s': 'geminal', 'use_jastrow': 'F'}, geminal=[])
    assert 'error' not in prepared, prepared
    written = (tmp_path / 'gem' / 'parameters.casl').read_text()
    assert 'GEMINAL:' in written and '      g_5,5: [ 1.0, fixed ]' in written
    assert 'parameters.casl (written)' in prepared['copied']
    assert prepared['geminal']['geminals'] == 1


def test_the_input_is_not_refused_for_the_parameters_casl_that_is_about_to_be_written(orbitals_only, tmp_path):
    """`psi_s : geminal` with no parameters.casl is a refusal -- unless it comes with one."""
    values: dict[str, str | None] = {'psi_s': 'geminal', 'use_jastrow': 'F'}
    assert 'error' in runtime.prepare(str(orbitals_only), str(tmp_path / 'a'), overrides=values)
    assert 'error' not in runtime.prepare(str(orbitals_only), str(tmp_path / 'b'), overrides=values, geminal=[])


def test_prepare_will_not_overwrite_a_geminal_that_already_exists(beryllium, tmp_path):
    """An optimized parameters.casl is an input, and it would be copied over."""
    (beryllium / 'parameters.casl').write_text('GEMINAL:\n')
    prepared = runtime.prepare(str(beryllium), str(tmp_path / 'next'), geminal=[])
    assert 'already has a parameters.casl' in prepared['error']
    assert not (tmp_path / 'next').exists()


def test_a_geminal_the_input_would_not_read_is_refused(orbitals_only, tmp_path):
    prepared = runtime.prepare(str(orbitals_only), str(tmp_path / 'gem'), geminal=[])
    assert any('psi_s : geminal' in problem for problem in prepared['problems'])
    assert not (tmp_path / 'gem').exists()


def test_prepare_correlates_the_levels_the_channels_name(beryllium, tmp_path):
    prepared = runtime.prepare(str(beryllium), str(tmp_path / 'gem'), geminal=['p:2'], geminal_settings={'anchors': [1]})
    assert 'error' not in prepared, prepared
    assert prepared['geminal']['shells'] == [[3, 5, 4], [8, 9, 7]]
    assert prepared['geminal']['levels']['p'] == 4  # what the file holds, not what was asked for
    written = (tmp_path / 'gem' / 'parameters.casl').read_text()
    assert '  Geminal 2:' in written
    assert '    2^g_4,4=2^g_5,5=2^g_3,3' in written.splitlines()


def test_the_geminal_settings_reach_the_file(beryllium, tmp_path):
    prepared = runtime.prepare(str(beryllium), str(tmp_path / 'gem'), geminal=['p:1'], geminal_settings={'seed': -0.19, 'mirror': 1})
    assert 'error' not in prepared, prepared
    written = (tmp_path / 'gem' / 'parameters.casl').read_text()
    assert '      g_4,4: [ -0.19, optimizable ]' in written
    assert '  Geminal 3:' in written


def test_an_unknown_geminal_setting_is_refused_by_name(beryllium, tmp_path):
    prepared = runtime.prepare(str(beryllium), str(tmp_path / 'gem'), geminal=[], geminal_settings={'wobble': 1})
    assert any('no such geminal setting' in problem for problem in prepared['problems'])


def test_a_channel_needs_an_orbital_file_this_can_read(beryllium, tmp_path):
    """A Slater-type or plane-wave calculation can still have the Hartree-Fock geminal."""
    prepared = runtime.prepare(str(beryllium), str(tmp_path / 'gem'), overrides={'atom_basis_type': 'slater-type'}, geminal=['p:1'])
    assert any('gwfn.data' in problem for problem in prepared['problems'])


def test_a_channel_the_orbital_file_cannot_answer_is_refused(beryllium, tmp_path):
    prepared = runtime.prepare(str(beryllium), str(tmp_path / 'gem'), geminal=['g:9'])
    assert any('g:9 was asked for' in problem for problem in prepared['problems'])
    assert not (tmp_path / 'gem').exists()


def test_a_prepared_directory_is_one_casino_run_will_accept(calculation, tmp_path, fake_runqmc, python_path):
    """The two halves meet here: what prepare writes is what start is willing to run."""
    fake_runqmc()
    prepared = runtime.prepare(str(calculation), str(tmp_path / 'next'), overrides={'dtdmc': '0.005'})
    assert 'error' not in prepared
    assert 'error' not in runtime.start(prepared['workdir'])


# --- defaults ------------------------------------------------------------------------


def test_defaults_are_the_documented_ones(workdir, fake_runqmc, python_path):
    fake_runqmc()
    started = runtime.start(str(workdir))
    assert started['command'].endswith(f'-p {settings.NPROC}')
    assert '--version' not in started['command']  # the default flavour is runqmc's own


def test_a_non_default_version_reaches_the_command_line(workdir, fake_runqmc, python_path):
    fake_runqmc()
    started = runtime.start(str(workdir), nproc=3, version='debug')
    assert started['command'].endswith('-p 3 --version=debug')
