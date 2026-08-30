"""The job registry: records on disk, so a calculation outlives the server that started it.

State lives outside the calculation directory (see `settings.state_path`); the
calculation directory only ever gets what CASINO itself puts there.
"""

import json
import random
import string
import time
from datetime import UTC, datetime
from pathlib import Path

from casino_mcp import settings


def state_dir() -> Path:
    path = settings.state_path()
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_dir() -> Path:
    path = state_dir() / 'jobs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_job_id() -> str:
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return datetime.now(UTC).astimezone().strftime('%Y%m%d-%H%M%S-') + suffix


def now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec='seconds')


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def proc_stat(pid: int):
    """(state, start time) from /proc/<pid>/stat, or None if there is no such process.

    Both fields are counted from after the comm field, which is in parentheses and may
    itself contain spaces: state is field 3 and start time field 22, 1-based.
    """
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    after_comm = stat[stat.rindex(')') + 2 :].split()
    return after_comm[0], int(after_comm[19])


def proc_start_time(pid: int):
    stat = proc_stat(pid)
    return None if stat is None else stat[1]


def proc_alive(pid: int, start_time) -> bool:
    """Whether this is still the process we started.

    A zombie is not alive: the launcher has exited and only its exit status is left for a
    parent that is not us to reap. Counting it as running would leave a job that was killed
    hard -- so with no status.json to read -- reported as running until the reboot.
    """
    stat = proc_stat(pid)
    if stat is None or stat[0] == 'Z':
        return False
    if start_time is None:
        return True
    return stat[1] == start_time


def binary_stamp(version: str) -> dict:
    """Path, size and mtime of the `casino` binary a job is about to run.

    Frozen into the job record so that results from before and after a rebuild can be
    told apart afterwards -- the one piece of build provenance the server is right to keep.
    """
    path = settings.binary_path(version)
    try:
        st = path.stat()
    except OSError:
        return {'path': str(path), 'exists': False}
    return {
        'path': str(path),
        'exists': True,
        'size': st.st_size,
        'mtime': datetime.fromtimestamp(st.st_mtime, UTC).astimezone().isoformat(timespec='seconds'),
    }


class JobStore:
    """Job records on disk, so a job outlives the MCP server that started it."""

    @property
    def index_path(self) -> Path:
        return state_dir() / 'jobs.json'

    def index(self) -> dict:
        return read_json(self.index_path) or {}

    def add(self, meta: dict) -> None:
        index = self.index()
        # `created_epoch` and not the id: a job id is only chronological to the second, and two
        # runs of the same directory a second apart are exactly what a scan does.
        index[meta['job_id']] = {'workdir': meta['workdir'], 'created': meta['created'], 'created_epoch': meta['created_epoch']}
        keep = settings.KEEP_JOBS
        if keep > 0 and len(index) > keep:
            # job ids sort chronologically, so the oldest records fall off the end
            index = {job_id: index[job_id] for job_id in sorted(index, reverse=True)[:keep]}
        write_json(self.index_path, index)

    def meta(self, job_id: str):
        return read_json(jobs_dir() / job_id / 'meta.json')

    def latest(self, workdir) -> str | None:
        """The newest job that ran in this directory, or None.

        The registry answers the question "what ran here", so that nothing has to be written
        into the calculation directory to record it: one directory is one calculation, and what
        is in it is what CASINO put there. Paths are compared resolved, so a symlinked or
        relative path finds the job that a different spelling of it started.
        """
        try:
            wanted = Path(workdir).expanduser().resolve()
        except OSError:
            return None
        index = self.index()
        here = [job_id for job_id, entry in index.items() if entry.get('workdir') and Path(entry['workdir']).resolve() == wanted]
        if not here:
            return None
        # a record written before `created_epoch` was kept falls back to its id, which orders
        # to the second and is all such a record has
        return max(here, key=lambda job_id: (index[job_id].get('created_epoch') or 0, job_id))

    def mark_stopped(self, job_id: str) -> None:
        """Record that this job was stopped on purpose.

        The exit code cannot say it: a job is stopped by signalling CASINO itself, and the
        `runqmc` that outlives it exits however it likes once its calculation has gone.
        """
        meta = self.meta(job_id)
        if meta is None:
            raise KeyError(job_id)
        meta['stopped'] = now()
        write_json(jobs_dir() / job_id / 'meta.json', meta)

    def status(self, job_id: str) -> dict:
        """Current state of a job, from the launcher's status file or from /proc."""
        meta = self.meta(job_id)
        if meta is None:
            raise KeyError(job_id)
        job_dir = jobs_dir() / job_id
        finished = read_json(job_dir / 'status.json')
        state = {
            'job_id': job_id,
            'workdir': meta['workdir'],
            'command': meta['command'],
            'nproc': meta['nproc'],
            'pid': meta['pid'],
            'started': meta['created'],
            'binary': meta.get('binary'),
        }
        if finished is None:
            alive = proc_alive(meta['pid'], meta.get('start_time'))
            if alive:
                state['status'] = 'running'
                state['runtime'] = round(time.time() - meta['created_epoch'], 1)
            else:
                # launcher gone without writing status.json: killed hard, or lost at reboot
                state['status'] = 'unknown'
                state['runtime'] = None
                state['note'] = 'launcher vanished without recording an exit code'
        else:
            state['status'] = 'finished' if finished['exit_code'] == 0 else 'failed'
            state['exit_code'] = finished['exit_code']
            state['finished'] = finished['finished']
            state['runtime'] = round(finished['finished_epoch'] - meta['created_epoch'], 1)
            if finished.get('signalled'):
                state['status'] = 'stopped'
        if meta.get('stopped') and state['status'] != 'running':
            state['status'] = 'stopped'
            state['stopped'] = meta['stopped']
        state['runqmc_log'] = str(job_dir / 'runqmc.log')
        return state

    def all_status(self) -> list:
        index = self.index()
        out = []
        for job_id in sorted(index, reverse=True):
            try:
                out.append(self.status(job_id))
            except KeyError:
                continue
        return out


def create(command: list[str], workdir: Path, nproc: int, version: str) -> tuple[str, Path, dict]:
    """A job directory with its frozen meta.json, before anything is spawned."""
    job_id = new_job_id()
    job_dir = jobs_dir() / job_id
    job_dir.mkdir(parents=True)
    meta = {
        'job_id': job_id,
        'workdir': str(workdir),
        'command': command,
        'nproc': nproc,
        'version': version,
        'created': now(),
        'created_epoch': time.time(),
        'binary': binary_stamp(version),
        'pid': None,
        'start_time': None,
    }
    write_json(job_dir / 'meta.json', meta)
    return job_id, job_dir, meta


def record_pid(job_dir: Path, meta: dict, pid: int) -> dict:
    """Stamp the launcher's pid and its /proc start time, so a recycled pid cannot pass for it."""
    meta['pid'] = pid
    meta['start_time'] = proc_start_time(pid)
    write_json(job_dir / 'meta.json', meta)
    return meta


__all__ = [
    'JobStore',
    'binary_stamp',
    'create',
    'jobs_dir',
    'new_job_id',
    'now',
    'proc_alive',
    'proc_start_time',
    'proc_stat',
    'read_json',
    'record_pid',
    'state_dir',
    'write_json',
]
