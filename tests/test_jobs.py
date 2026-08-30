"""The registry: ids, atomic writes, liveness, retention, and the states a job can be in.

Nothing here starts a calculation. A job record is just a directory, so every status this
module can report is reachable by writing the files a launcher would have written -- which
is also how the hard case is tested: a launcher that died without recording an exit code.
"""

import json
import os
import subprocess
import sys
import time

import pytest

from casino_mcp import jobs, settings


def make_job(store, workdir, pid=None, created_epoch=None, **extra):
    """A job record as `runtime.start` would leave it, without spawning anything."""
    job_id, job_dir, meta = jobs.create(['runqmc', '-p', '1'], workdir, nproc=1, version='opt')
    meta['pid'] = os.getpid() if pid is None else pid
    meta['start_time'] = jobs.proc_start_time(meta['pid'])
    if created_epoch is not None:
        meta['created_epoch'] = created_epoch
    meta.update(extra)
    jobs.write_json(job_dir / 'meta.json', meta)
    store.add(meta)
    return job_id, job_dir


def test_job_id_is_unique_and_sorts_chronologically():
    """Retention and `all_status` both lean on ids sorting the way the clock runs."""
    ids = {jobs.new_job_id() for _ in range(50)}
    assert len(ids) == 50
    for job_id in ids:
        stamp, _, suffix = job_id.rpartition('-')
        assert len(stamp) == len('20260823-164600') and len(suffix) == 4
    assert '20000101-000000-0000' < min(ids) < '20990101-000000-0000'


def test_state_dir_is_created_under_xdg(tmp_path):
    assert jobs.state_dir() == tmp_path / 'state' / 'casino-mcp'
    assert jobs.jobs_dir().is_dir()


def test_state_dir_never_touches_the_calculation(workdir):
    """The calculation directory only ever gets what CASINO puts there."""
    before = set(workdir.iterdir())
    jobs.create(['runqmc'], workdir, nproc=1, version='opt')
    assert set(workdir.iterdir()) == before


def test_write_json_is_atomic(tmp_path):
    """A reader must see the old file or the new one, never a half-written one."""
    path = tmp_path / 'index.json'
    jobs.write_json(path, {'a': 1})
    jobs.write_json(path, {'a': 2, 'b': [1, 2, 3]})
    assert json.loads(path.read_text()) == {'a': 2, 'b': [1, 2, 3]}
    assert list(tmp_path.glob('*.tmp')) == []


def test_read_json_survives_a_missing_or_broken_file(tmp_path):
    assert jobs.read_json(tmp_path / 'nowhere.json') is None
    broken = tmp_path / 'broken.json'
    broken.write_text('{not json')
    assert jobs.read_json(broken) is None


def test_proc_start_time_distinguishes_a_recycled_pid():
    """Without the start-time check, a recycled pid reports a finished job as running."""
    mine = os.getpid()
    assert jobs.proc_start_time(mine) is not None
    assert jobs.proc_alive(mine, jobs.proc_start_time(mine)) is True
    assert jobs.proc_alive(mine, jobs.proc_start_time(mine) + 1000) is False

    dead = subprocess.Popen([sys.executable, '-c', ''])
    dead.wait()
    assert jobs.proc_alive(dead.pid, 12345) is False


def test_binary_stamp_records_absence_without_inventing_a_path(monkeypatch, tmp_path):
    monkeypatch.setenv('CASINO_HOME', str(tmp_path))
    monkeypatch.setenv('CASINO_ARCH', 'linuxpc-gcc')
    stamp = jobs.binary_stamp('opt')
    assert stamp == {'path': str(tmp_path / 'bin_qmc' / 'linuxpc-gcc' / 'opt' / 'casino'), 'exists': False}


def test_binary_stamp_records_size_and_mtime(monkeypatch, tmp_path):
    binary = tmp_path / 'bin_qmc' / 'linuxpc-gcc' / 'opt' / 'casino'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'\x7fELF' + b'0' * 100)
    monkeypatch.setenv('CASINO_HOME', str(tmp_path))
    monkeypatch.setenv('CASINO_ARCH', 'linuxpc-gcc')

    stamp = jobs.binary_stamp('opt')
    assert stamp['exists'] is True and stamp['size'] == 104
    assert stamp['mtime'].startswith('20')


def test_status_of_an_unknown_job_raises_keyerror():
    with pytest.raises(KeyError):
        jobs.JobStore().status('20260823-000000-zzzz')


def test_running_job_reports_a_runtime(workdir):
    store = jobs.JobStore()
    job_id, _ = make_job(store, workdir, created_epoch=time.time() - 30)

    state = store.status(job_id)
    assert state['status'] == 'running'
    assert 29 <= state['runtime'] <= 31
    assert state['workdir'] == str(workdir)
    assert state['runqmc_log'].endswith(f'{job_id}/runqmc.log')


@pytest.mark.parametrize(
    'status_json,expected',
    [
        ({'exit_code': 0, 'signalled': False}, 'finished'),
        ({'exit_code': 1, 'signalled': False}, 'failed'),
        ({'exit_code': 143, 'signalled': True}, 'stopped'),
        ({'exit_code': 0, 'signalled': True}, 'stopped'),  # a stop that landed between blocks
    ],
)
def test_finished_states_come_from_the_launcher(workdir, status_json, expected):
    store = jobs.JobStore()
    started = time.time() - 12
    job_id, job_dir = make_job(store, workdir, created_epoch=started)
    jobs.write_json(job_dir / 'status.json', {**status_json, 'finished': jobs.now(), 'finished_epoch': started + 12})

    state = store.status(job_id)
    assert state['status'] == expected
    assert state['exit_code'] == status_json['exit_code']
    assert state['runtime'] == 12.0


def test_launcher_that_vanished_is_unknown_not_finished(workdir):
    """A hard kill or a reboot leaves no status.json. Calling that 'finished' would be a lie."""
    store = jobs.JobStore()
    dead = subprocess.Popen([sys.executable, '-c', ''])
    dead.wait()
    job_id, _ = make_job(store, workdir, pid=dead.pid)

    state = store.status(job_id)
    assert state['status'] == 'unknown'
    assert state['runtime'] is None
    assert 'exit code' in state['note']


def test_all_status_is_newest_first(workdir):
    store = jobs.JobStore()
    ids = [make_job(store, workdir)[0] for _ in range(3)]
    assert [state['job_id'] for state in store.all_status()] == sorted(ids, reverse=True)


def test_running_is_the_jobs_that_are_still_going(workdir):
    store = jobs.JobStore()
    live, _ = make_job(store, workdir)
    ended, ended_dir = make_job(store, workdir)
    jobs.write_json(ended_dir / 'status.json', {'exit_code': 0, 'finished': 'x', 'finished_epoch': time.time()})
    gone, _ = make_job(store, workdir, pid=999999, **{'start_time': None})

    assert [state['job_id'] for state in store.running()] == [live]
    assert {state['job_id'] for state in store.all_status()} == {live, ended, gone}


def test_running_does_not_read_the_jobs_that_have_ended(workdir, monkeypatch):
    """`casino_run` asks this on every call, and a full registry is KEEP_JOBS records.

    A launcher that wrote `status.json` has ended, and finding that out is one stat call: the
    meta and the /proc lookup are what this must not pay for the ones that are over.
    """
    store = jobs.JobStore()
    for _ in range(5):
        _, job_dir = make_job(store, workdir)
        jobs.write_json(job_dir / 'status.json', {'exit_code': 0, 'finished': 'x', 'finished_epoch': time.time()})
    live, _ = make_job(store, workdir)

    read = []
    monkeypatch.setattr(jobs.JobStore, 'meta', lambda self, job_id: read.append(job_id) or jobs.read_json(jobs.jobs_dir() / job_id / 'meta.json'))
    assert [state['job_id'] for state in store.running()] == [live]
    assert read == [live], 'the five that ended were passed over on the stat alone'


def test_a_job_whose_directory_is_gone_is_skipped_not_fatal(workdir):
    store = jobs.JobStore()
    job_id, job_dir = make_job(store, workdir)
    make_job(store, workdir)
    (job_dir / 'meta.json').unlink()

    listed = store.all_status()
    assert len(listed) == 1 and listed[0]['job_id'] != job_id


def test_index_is_trimmed_to_keep(workdir, monkeypatch):
    monkeypatch.setattr(settings, 'KEEP_JOBS', 3)
    store = jobs.JobStore()

    ids = [make_job(store, workdir)[0] for _ in range(6)]
    assert sorted(store.index()) == sorted(ids)[-3:]
    # only the index is trimmed; the per-job directories, and their logs, stay on disk
    assert len(list(jobs.jobs_dir().iterdir())) == 6


def test_keep_zero_keeps_everything(workdir, monkeypatch):
    monkeypatch.setattr(settings, 'KEEP_JOBS', 0)
    store = jobs.JobStore()

    for _ in range(5):
        make_job(store, workdir)
    assert len(store.index()) == 5


def test_registry_survives_a_new_store_object(workdir):
    """The whole point of the on-disk registry: a job outlives the server that started it."""
    job_id, _ = make_job(jobs.JobStore(), workdir)
    assert jobs.JobStore().status(job_id)['job_id'] == job_id


def test_create_freezes_the_command_and_the_binary(workdir):
    job_id, job_dir, meta = jobs.create(['runqmc', '-p', '4'], workdir, nproc=4, version='debug')
    on_disk = json.loads((job_dir / 'meta.json').read_text())
    assert on_disk == meta
    assert on_disk['command'] == ['runqmc', '-p', '4']
    assert on_disk['version'] == 'debug'
    assert on_disk['binary']['path'].endswith('/debug/casino')
    assert on_disk['pid'] is None and on_disk['job_id'] == job_id


def test_record_pid_stamps_the_start_time(workdir):
    _, job_dir, meta = jobs.create(['runqmc'], workdir, nproc=1, version='opt')
    meta = jobs.record_pid(job_dir, meta, os.getpid())
    assert meta['pid'] == os.getpid()
    assert meta['start_time'] == jobs.proc_start_time(os.getpid())
    assert json.loads((job_dir / 'meta.json').read_text())['start_time'] == meta['start_time']
