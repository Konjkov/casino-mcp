"""Starting, watching and stopping CASINO runs. No MCP in this module.

`runqmc` is the runtime: it is a 3900-line bash script that already knows arch detection,
MPI variants, batch-queue submission, `--auto-continue` and the lock file. This layer goes
*over* it and adds only what it does not do -- a record of what was run, an exit code that
survives the caller, and a session of its own by which the run's processes can be found.

Every state change to a calculation goes through CASINO's own scripts, and only through
them: `runqmc` starts a run, `haltqmc` ends one and tidies the directory afterwards, and
either `runqmc --continue` or a plain `runqmc` over the `input` that `haltqmc -u` rewrote
carries it on. Nothing here moves a config file, edits an `input` or decides what a
half-finished calculation should do next -- CASINO has programs for all of that.

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
UPDATE_HELPER = 'haltqmc_update_input'  # the program `haltqmc -u` rewrites `input` with
CASINO_COMM = 'casino'  # what a CASINO process is called in /proc, and what `haltqmc -k` pkills
ESCALATION_GRACE = 5.0  # seconds each further signal gets once the polite one has been ignored

# Markers in `out`. `Started` opens every CASINO run, so it is what splits a file that
# several runs have been appended to; the other two say how the last of them ended.
SEGMENT_MARKER = 'Started '
CONTINUATION_MARKER = 'CONTINUATION INFO:'
FINISHED_MARKER = 'Total CASINO CPU time'

# What an earlier run left behind, and what `restart` therefore deletes. `runqmc` appends to
# `out` and to the `.hist` files rather than replacing them, so a re-run in a dirty directory
# produces a file that is two runs glued together -- the flag exists to prevent exactly that.
#
# The list is named, not "everything that is not an input": the same directory holds the wave
# function, the pseudopotentials and a `correlation.data` that is often hand-edited, and any
# glob wide enough to catch every product would catch those too. Where a bare name is an input
# and a numbered one is a product, only the numbered form is here: `parameters.4.casl` is what
# an optimisation cycle wrote, `parameters.casl` is what it started from.
DEBRIS = (
    'out',
    'out_part.[0-9]*',  # earlier segments, put aside by a previous --continue
    '.out_proc*',  # per-process output a killed run never got to concatenate into `out`
    'vmc.hist',
    'dmc.hist',
    '*.hist.[0-9]*',
    'config.in',  # continuation state: deleting it is what makes this a restart and not a resume
    'config.out',
    'config.in_*',  # _fixed / _nofixed, written by a fixed-node run
    'config.out_*',
    'config.out.[0-9]*',
    'correlation.out',
    'correlation.out.[0-9]*',
    'parameters.[0-9]*.casl',
    'saved_part_[0-9]*',  # a directory: what runqmc moved aside when it set up a continuation
)


def find_script(name: str, override: str) -> str | None:
    """The named CASINO script: its own variable, else one on PATH, else $CASINO_HOME/bin_qmc.

    An explicit override that is not executable is not silently replaced by another one:
    falling back to PATH there would run a different script than the one named.
    """
    if override:
        explicit = Path(override).expanduser()
        return str(explicit) if os.access(explicit, os.X_OK) else None
    found = shutil.which(name)
    if found is not None:
        return found
    fallback = settings.casino_home() / 'bin_qmc' / name
    if os.access(fallback, os.X_OK):
        return str(fallback)
    return None


def find_runqmc() -> str | None:
    """$CASINO_RUNQMC, else one on PATH, else the one under $CASINO_HOME."""
    return find_script('runqmc', settings.runqmc_override())


def find_haltqmc() -> str | None:
    """$CASINO_HALTQMC, else one on PATH, else the one under $CASINO_HOME."""
    return find_script('haltqmc', settings.haltqmc_override())


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


def clear_debris(path: Path) -> list[str]:
    """Delete every product of an earlier run in `path` and name what went.

    Only the patterns in DEBRIS, so `input`, the wave function, the pseudopotentials,
    `correlation.data`, `parameters.casl` and the lock file all survive.
    """
    removed = []
    for pattern in DEBRIS:
        for victim in sorted(path.glob(pattern)):
            if victim.is_dir():
                shutil.rmtree(victim, ignore_errors=True)
            else:
                victim.unlink(missing_ok=True)
            removed.append(victim.name)
    return removed


def last_run(out: Path) -> list[str]:
    """The lines of the last CASINO run in `out`.

    Both continuation routes append rather than replace -- `runqmc --continue` puts the
    finished segment aside as `out_part.N` but a halted-and-continued run leaves several
    CASINO runs in one file -- and only the last of them says how the calculation stands.
    """
    lines = out.read_text(errors='replace').splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().startswith(SEGMENT_MARKER)]
    return lines[starts[-1] :] if starts else lines


def resume_mode(out: Path) -> str:
    """How the last run in `out` ended, and therefore how CASINO continues it.

    'continue': CASINO stopped itself on max_cpu_time / max_real_time and wrote the
        continuation info that `runqmc --continue` reads -- `Set NEWRUN : F`, which files to
        move -- and applies for us.
    'halted': the run was interrupted before it could write any of that. What continues it
        is the `input` that `haltqmc -u` rewrote when the job was stopped, run by a plain
        `runqmc`; `--continue` would only errstop on the missing continuation info.
    'complete': it ran to its own end. There is nothing to continue.
    """
    segment = last_run(out)
    if any(CONTINUATION_MARKER in line for line in segment):
        return 'continue'
    if any(line.strip().startswith(FINISHED_MARKER) for line in segment):
        return 'complete'
    return 'halted'


def set_to_continue(inp: Path) -> bool:
    """Whether `input` says NEWRUN : F, i.e. is set up to carry an earlier run on.

    That is what `haltqmc -u` leaves behind, and it is the one thing `restart` cannot undo:
    CASINO refuses NEWRUN : F without the `config.in` that restarting has just deleted.
    """
    for line in inp.read_text(errors='replace').splitlines():
        keyword, _, rest = line.partition(':')
        if keyword.strip().lower() == 'newrun':
            return rest.split('#')[0].strip().upper() in ('F', 'FALSE', '.FALSE.')
    return False


def check_workdir(path: Path, restart: bool, resume: bool, unlock: bool) -> str:
    """The reason this directory must not be run in, or '' if it may be.

    The guard is semantic, not syntactic: the risk is not a shell metacharacter, it is
    destroying an `out` file that other work is validated against.
    """
    if restart and resume:
        return 'restart and resume are opposites: restart deletes what the earlier run left, resume carries on from it. Pass one.'
    if not path.is_dir():
        return f'not a directory: {path}'
    if not (path / 'input').is_file():
        return f'no CASINO `input` file in {path}'

    forbidden = settings.forbidden(path)
    if forbidden:
        return f'{path} is under {forbidden}, which $CASINO_MCP_FORBID lists. No override exists; copy the directory elsewhere and run there.'

    if restart and set_to_continue(path / 'input'):
        return (
            f'{path / "input"} says NEWRUN : F -- it is set up to continue an earlier run, which is what '
            f'haltqmc leaves behind when a job is stopped. Restarting deletes the `config.in` that CASINO '
            f'then requires, and the run would fail. Put back the input this calculation started from '
            f'(casino_stop keeps a copy of it in the job directory), or pass resume=true to carry it on.'
        )

    lock = path / LOCK_NAME
    if lock.exists() and not unlock:
        return f'{lock} exists: another run holds this directory. Pass unlock=true if it is stale.'

    out = path / 'out'
    if resume and not out.is_file():
        return f'no `out` in {path}: resume continues an interrupted run from the continuation info its output ends with, and there is none here.'
    if out.exists() and not (restart or resume):
        if git_tracked(out):
            return f'{out} is committed reference data. Copy the directory and run there, or pass restart=true to delete it and start over.'
        return (
            f'{out} holds an earlier run, and runqmc appends to it rather than replacing it. '
            f'Run in a fresh directory, or pass restart=true to delete what that run left, or resume=true to carry it on.'
        )
    return ''


def build_command(runqmc: str, nproc: int, version: str, unlock: bool, resume: bool = False, extra: tuple[str, ...] = ()) -> list[str]:
    command = [runqmc, '-p', str(nproc)]
    if version != settings.VERSION:
        command.append(f'--version={version}')
    if unlock:
        command.append('--unlock')
    if resume:
        # runqmc reads the CONTINUATION INFO block at the end of `out`, moves the finished
        # segment into saved_part_N/ and edits `input` itself. None of that is ours to do.
        command.append('--continue')
    return command + list(extra)


def start(
    workdir: str,
    nproc: int = settings.NPROC,
    version: str = settings.VERSION,
    restart: bool = False,
    resume: bool = False,
    unlock: bool = False,
    store: jobs.JobStore | None = None,
) -> dict[str, Any]:
    """Spawn a launcher for one runqmc run and return its job record. Does not wait."""
    store = store or jobs.JobStore()
    if nproc < 1:
        return {'error': f'nproc must be at least 1, got {nproc}'}

    path = Path(workdir).expanduser().resolve()
    refusal = check_workdir(path, restart, resume, unlock)
    if refusal:
        return {'error': refusal}

    runqmc = find_runqmc()
    if runqmc is None:
        return {'error': f'runqmc not found: not in $CASINO_RUNQMC, not on PATH, not in {settings.casino_home() / "bin_qmc"}'}

    # Which of CASINO's two continuation routes this directory is in is not ours to choose:
    # `out` already says, and asking for the wrong one is an errstop from runqmc.
    mode = resume_mode(path / 'out') if resume else ''
    if mode == 'complete':
        return {
            'error': (
                f'the last run in {path / "out"} reached its own end, so there is nothing to continue. '
                f'Pass restart=true to run it again from the beginning, or run in a fresh directory.'
            )
        }

    # After the last thing that can refuse, never before: a directory is not emptied for a run
    # that then fails to start.
    removed = clear_debris(path) if restart else []
    command = build_command(runqmc, nproc, version, unlock, resume=mode == 'continue')
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

    started = {
        'job_id': job_id,
        'pid': process.pid,
        'workdir': str(path),
        'command': ' '.join(command),
        'started': meta['created'],
        'binary': meta['binary'],
        'removed': removed,  # named, so a restart that ate more than expected is visible in the reply
    }
    if resume:
        started['resume'] = mode
        if mode == 'halted':
            started['note'] = (
                'no continuation info in `out`: this run was interrupted rather than stopped by a time limit, '
                'so it continues from the `input` that `haltqmc -u` rewrote, not with `runqmc --continue`. '
                'If the job was not stopped through casino_stop, run haltqmc -f -u there first.'
            )
    return started


def status(job_id: str, store: jobs.JobStore | None = None) -> dict[str, Any]:
    store = store or jobs.JobStore()
    try:
        return store.status(job_id)
    except KeyError:
        return {'error': f'unknown job {job_id}'}


def listing(limit: int = 20, store: jobs.JobStore | None = None) -> dict[str, Any]:
    store = store or jobs.JobStore()
    return {'jobs': store.all_status()[:limit]}


def casino_processes(session: int) -> list[int]:
    """The `casino` processes belonging to one job, by session id.

    This is `haltqmc -k`'s own target -- it runs `pkill -x -u $USER casino` -- narrowed from
    every CASINO process the user owns to the ones this job started, because a server that
    runs several calculations at once must not have one stop take the others down with it.

    The session, not the process group: `mpirun` puts every rank in a process group of its
    own, so the group that `killpg` reaches holds runqmc and mpirun but not one `casino`.
    The session is the launcher's, and it is what the whole tree shares.
    """
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text()
        except OSError:
            continue  # the process ended between the listing and the read
        comm = stat[stat.index('(') + 1 : stat.rindex(')')]
        fields = stat[stat.rindex(')') + 2 :].split()  # state, ppid, pgrp, session, ...
        if comm == CASINO_COMM and int(fields[3]) == session:
            found.append(int(entry.name))
    return sorted(found)


def clear_lock(path: Path) -> bool:
    """Remove a lock file haltqmc did not get to, and say whether there was one."""
    lock = path / LOCK_NAME
    if not lock.exists():
        return False
    lock.unlink()
    return True


def tail(text: str, lines: int = 12) -> str:
    return '\n'.join(text.strip().splitlines()[-lines:])


def halt(path: Path, keep_input: Path | None = None, timeout: float = settings.HALT_TIMEOUT) -> dict[str, Any]:
    """Hand the directory to CASINO's own haltqmc once the job has ended, and report what it did.

    `-f` because the killed run's `.runqmc.lock` is still there, `-u` because rewriting
    `input` -- NEWRUN to F, the blocks already done subtracted, the runtype moved on -- is
    what makes the next `runqmc` continue this calculation instead of redoing it. Not `-k`:
    the job is already dead by the time this runs, and haltqmc's kill is a `pkill casino`
    over the whole account. Not `-r` either: reblocking is a results question, and haltqmc
    errstops on a run with no statistics to reblock.

    haltqmc looks `haltqmc_update_input` up on PATH, so its own directory goes on there --
    a CASINO installation that is not on PATH still has the helper next to the script.

    `keep_input` is where the `input` haltqmc is about to rewrite is copied first. It belongs
    in the job directory and never in the calculation directory: keeping the file the run was
    started from is provenance, which is this layer's business, while what the calculation
    directory holds stays CASINO's.
    """
    haltqmc = find_haltqmc()
    if haltqmc is None:
        return {
            'error': f'haltqmc not found: not in $CASINO_HALTQMC, not on PATH, not in {settings.casino_home() / "bin_qmc"}',
            'note': 'the directory was left as the killed run left it; run haltqmc -f -u there by hand before continuing it',
            'lock_cleared': clear_lock(path),
        }

    env = os.environ.copy()
    env['PATH'] = str(Path(haltqmc).resolve().parent) + os.pathsep + env.get('PATH', '')
    helper = shutil.which(UPDATE_HELPER, path=env['PATH'])
    command = [haltqmc, '-f'] + (['-u'] if helper else [])
    saved = None
    if helper and keep_input is not None and (path / 'input').is_file():
        shutil.copy2(path / 'input', keep_input)
        saved = str(keep_input)
    try:
        result = subprocess.run(command, cwd=path, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'command': ' '.join(command), 'error': f'haltqmc did not run: {e}', 'lock_cleared': clear_lock(path)}

    report = {
        'command': ' '.join(command),
        'exit_code': result.returncode,
        'updated_input': helper is not None and result.returncode == 0,
        'input_saved': saved,  # the input as it was before haltqmc rewrote it, or None
        'output': tail(result.stdout + result.stderr),
    }
    if helper is None:
        report['note'] = (
            f'{UPDATE_HELPER} is not on PATH and not beside haltqmc, so `input` was not updated: this run can be restarted but not continued.'
        )
    elif result.returncode != 0:
        report['error'] = 'haltqmc exited nonzero: the directory may not have been tidied and `input` may not have been updated'
    report['lock_cleared'] = clear_lock(path)  # only if haltqmc left one, which it does not when it succeeds
    return report


def wait_while_running(job_id: str, store: jobs.JobStore, deadline: float) -> dict[str, Any]:
    while time.time() < deadline:
        state = store.status(job_id)
        if state['status'] != 'running':
            return state
        time.sleep(0.5)
    return store.status(job_id)


def stop(job_id: str, timeout: float = settings.STOP_TIMEOUT, store: jobs.JobStore | None = None) -> dict[str, Any]:
    """Stop a running calculation the way CASINO stops one, then let haltqmc tidy up.

    SIGTERM goes to this job's `casino` processes and to nothing else -- the same signal
    `haltqmc -k` sends -- which leaves `runqmc` alive to finish its own epilogue: the
    per-node output concatenated into `out`, its lock file removed. Only if the job is still
    running after `timeout` is the whole process group signalled, and then killed.

    Once the job has ended, `haltqmc -f -u` gets the directory: config.out to config.in, the
    marker files, and `input` rewritten so that a later casino_run(resume=true) continues
    this calculation rather than starting it again.
    """
    store = store or jobs.JobStore()
    try:
        state = store.status(job_id)
    except KeyError:
        return {'error': f'unknown job {job_id}'}
    if state['status'] != 'running':
        return {'job_id': job_id, 'status': state['status'], 'note': 'not running, nothing to stop'}

    pid = state['pid']
    targets = casino_processes(pid)  # the launcher leads its own session, so its pid is the sid
    for target in targets:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass  # it ended on its own between the listing and the signal
    if not targets:
        # Nothing named `casino` in the session: the run is still in runqmc's setup -- arch
        # detection, the lock, copying files -- or it is already over. Either way the process
        # group is what there is to signal.
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError as e:
            return {'error': f'could not signal job {job_id} (pid {pid}): {e}'}

    state = wait_while_running(job_id, store, time.time() + timeout)
    for escalation in (signal.SIGTERM, signal.SIGKILL):
        if state['status'] != 'running':
            break
        try:
            os.killpg(pid, escalation)
        except OSError:
            pass
        state = wait_while_running(job_id, store, time.time() + ESCALATION_GRACE)

    store.mark_stopped(job_id)
    state = store.status(job_id)
    state['terminated'] = {'scope': 'casino' if targets else 'process group', 'pids': targets or [pid]}
    state['halt'] = halt(Path(state['workdir']), keep_input=jobs.jobs_dir() / job_id / 'input.before_halt')
    return state
