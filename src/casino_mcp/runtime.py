"""Starting, watching and stopping CASINO runs. No MCP in this module.

`runqmc` is the runtime: it is a 3900-line bash script that already knows arch detection,
MPI variants, batch-queue submission, `--auto-continue` and the lock file. This layer goes
*over* it and adds only what it does not do -- a record of what was run, an exit code that
survives the caller, and a process group that can be signalled as a unit.

Every function returns a JSON-able dict. A refusal is `{'error': ...}` carrying the reason
and the fix, never a silently safer action than the one that was asked for.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from casino_mcp import jobs, settings

LOCK_NAME = '.runqmc.lock'


def find_runqmc() -> str | None:
    """$CASINO_RUNQMC, else one on PATH, else the one under $CASINO_HOME.

    An explicit $CASINO_RUNQMC that is not executable is not silently replaced by another
    one: falling back to PATH there would run a different binary than the one named.
    """
    override = settings.runqmc_override()
    if override:
        explicit = Path(override).expanduser()
        return str(explicit) if os.access(explicit, os.X_OK) else None
    found = shutil.which('runqmc')
    if found is not None:
        return found
    fallback = settings.casino_home() / 'bin_qmc' / 'runqmc'
    if os.access(fallback, os.X_OK):
        return str(fallback)
    return None


def git_tracked(path: Path) -> bool:
    """Whether git tracks this file -- i.e. whether it is committed reference data."""
    try:
        result = subprocess.run(
            ['git', '-C', str(path.parent), 'ls-files', '--error-unmatch', path.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def check_workdir(path: Path, overwrite: bool, unlock: bool) -> str:
    """The reason this directory must not be run in, or '' if it may be.

    The guard is semantic, not syntactic: the risk is not a shell metacharacter, it is
    destroying an `out` file that other work is validated against.
    """
    if not path.is_dir():
        return f'not a directory: {path}'
    if not (path / 'input').is_file():
        return f'no CASINO `input` file in {path}'

    forbidden = settings.forbidden(path)
    if forbidden:
        return f'{path} is under {forbidden}, which $CASINO_MCP_FORBID lists. No override exists; copy the directory elsewhere and run there.'

    lock = path / LOCK_NAME
    if lock.exists() and not unlock:
        return f'{lock} exists: another run holds this directory. Pass unlock=true if it is stale.'

    out = path / 'out'
    if out.exists() and not overwrite:
        if git_tracked(out):
            return f'{out} is committed reference data. Copy the directory and run there, or pass overwrite=true to destroy it.'
        return f'{out} holds the results of an earlier run. Run in a fresh directory, or pass overwrite=true.'
    return ''


def build_command(runqmc: str, nproc: int, version: str, unlock: bool, extra: tuple[str, ...] = ()) -> list[str]:
    command = [runqmc, '-p', str(nproc)]
    if version != settings.VERSION:
        command.append(f'--version={version}')
    if unlock:
        command.append('--unlock')
    return command + list(extra)


def start(
    workdir: str,
    nproc: int = settings.NPROC,
    version: str = settings.VERSION,
    overwrite: bool = False,
    unlock: bool = False,
    store: jobs.JobStore | None = None,
) -> dict[str, Any]:
    """Spawn a launcher for one runqmc run and return its job record. Does not wait."""
    store = store or jobs.JobStore()
    if nproc < 1:
        return {'error': f'nproc must be at least 1, got {nproc}'}

    path = Path(workdir).expanduser().resolve()
    refusal = check_workdir(path, overwrite, unlock)
    if refusal:
        return {'error': refusal}

    runqmc = find_runqmc()
    if runqmc is None:
        return {'error': f'runqmc not found: not in $CASINO_RUNQMC, not on PATH, not in {settings.casino_home() / "bin_qmc"}'}

    command = build_command(runqmc, nproc, version, unlock)
    job_id, job_dir, meta = jobs.create(command, path, nproc, version)

    process = subprocess.Popen(
        [sys.executable, '-m', 'casino_mcp.launcher', str(job_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    meta = jobs.record_pid(job_dir, meta, process.pid)
    store.add(meta)

    return {
        'job_id': job_id,
        'pid': process.pid,
        'workdir': str(path),
        'command': ' '.join(command),
        'started': meta['created'],
        'binary': meta['binary'],
    }


def status(job_id: str, store: jobs.JobStore | None = None) -> dict[str, Any]:
    store = store or jobs.JobStore()
    try:
        return store.status(job_id)
    except KeyError:
        return {'error': f'unknown job {job_id}'}


def listing(limit: int = 20, store: jobs.JobStore | None = None) -> dict[str, Any]:
    store = store or jobs.JobStore()
    return {'jobs': store.all_status()[:limit]}


def stop(job_id: str, timeout: float = settings.STOP_TIMEOUT, store: jobs.JobStore | None = None) -> dict[str, Any]:
    """SIGTERM the launcher's process group, SIGKILL it after `timeout`, clear the stale lock."""
    store = store or jobs.JobStore()
    try:
        state = store.status(job_id)
    except KeyError:
        return {'error': f'unknown job {job_id}'}
    if state['status'] != 'running':
        return {'job_id': job_id, 'status': state['status'], 'note': 'not running, nothing to stop'}

    pid = state['pid']
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError as e:
        return {'error': f'could not signal job {job_id} (pid {pid}): {e}'}

    deadline = time.time() + timeout
    while time.time() < deadline:
        state = store.status(job_id)
        if state['status'] != 'running':
            break
        time.sleep(0.5)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(1.0)
        state = store.status(job_id)

    lock = Path(state['workdir']) / LOCK_NAME
    if lock.exists():
        lock.unlink()
    return state
