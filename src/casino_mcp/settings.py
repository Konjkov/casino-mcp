"""Where CASINO is, and where our own state goes.

An MCP server is configured where it is registered -- the `env` block of `.mcp.json` -- so
that is the channel this reads, and there is no configuration file of our own. CASINO's own
variables keep their names and meanings: `$CASINO_HOME` and `$CASINO_ARCH` are the ones
`runqmc` and the CASINO scripts already use, so setting them once configures both layers.

Everything else is a constant below. A knob is added here only when someone needs it to
differ between two runs on the same machine -- until then it is a default, and a default
belongs in one place.
"""

import os
from pathlib import Path

NPROC = 1  # MPI processes when a tool is not told otherwise
VERSION = 'opt'  # binary flavour: the directory under bin_qmc/<arch>
STOP_TIMEOUT = 20.0  # seconds a stopped job is given to end on its own before it is killed
HALT_TIMEOUT = 60.0  # seconds haltqmc gets to tidy the directory after the job has ended
KEEP_JOBS = 200  # job records kept in the index; the per-job directories are never trimmed

ENVIRONMENT = (
    ('CASINO_HOME', 'root of the CASINO installation'),
    ('CASINO_ARCH', 'build target: the directory under bin_qmc, used to stamp which binary ran'),
    ('CASINO_RUNQMC', 'explicit path to runqmc; otherwise PATH, then $CASINO_HOME/bin_qmc/runqmc'),
    ('CASINO_HALTQMC', 'explicit path to haltqmc; otherwise PATH, then $CASINO_HOME/bin_qmc/haltqmc'),
    ('CASINO_MCP_STATE_DIR', 'the job registry; otherwise $XDG_STATE_HOME/casino-mcp'),
    ('CASINO_MCP_FORBID', f'directories no run may ever touch, separated by {os.pathsep!r}'),
)


def casino_home() -> Path:
    return Path(os.environ.get('CASINO_HOME') or Path.home() / 'bin' / 'CASINO').expanduser()


def casino_arch() -> str:
    return os.environ.get('CASINO_ARCH', '')


def runqmc_override() -> str:
    return os.environ.get('CASINO_RUNQMC', '')


def haltqmc_override() -> str:
    return os.environ.get('CASINO_HALTQMC', '')


def binary_path(version: str) -> Path:
    """The `casino` binary a job is about to run, for the provenance stamp in its record."""
    return casino_home() / 'bin_qmc' / casino_arch() / version / 'casino'


def state_path() -> Path:
    """The job registry, always outside any calculation directory."""
    explicit = os.environ.get('CASINO_MCP_STATE_DIR')
    if explicit:
        return Path(explicit).expanduser()
    root = os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state'
    return Path(root).expanduser() / 'casino-mcp'


def forbidden(path: Path) -> str:
    """The $CASINO_MCP_FORBID entry that covers `path`, or '' if none does.

    Unlike `restart` and `unlock`, this one has no per-call override: it is for the
    directories -- committed reference calculations, someone else's results -- that no
    argument should be able to unlock.
    """
    for entry in os.environ.get('CASINO_MCP_FORBID', '').split(os.pathsep):
        if not entry:
            continue
        root = Path(entry).expanduser().resolve()
        if path == root or root in path.parents:
            return str(root)
    return ''


def resolved() -> dict:
    """What the settings currently are and which variable said so, for `casino-mcp config`."""
    return {
        'casino_home': str(casino_home()),
        'casino_arch': casino_arch(),
        'runqmc': runqmc_override(),
        'haltqmc': haltqmc_override(),
        'state_dir': str(state_path()),
        'forbid': [entry for entry in os.environ.get('CASINO_MCP_FORBID', '').split(os.pathsep) if entry],
        'defaults': {
            'nproc': NPROC,
            'version': VERSION,
            'stop_timeout': STOP_TIMEOUT,
            'halt_timeout': HALT_TIMEOUT,
            'keep_jobs': KEEP_JOBS,
        },
        'environment': {name: os.environ.get(name) for name, _ in ENVIRONMENT},
    }
