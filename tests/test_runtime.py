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

from casino_mcp import jobs, runtime, settings

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
