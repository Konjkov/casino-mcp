"""MCP server driving the Fortran CASINO code.

Tools are named CASINO operations; there is deliberately no generic shell tool. Everything
these tools do lives in `runtime.py` and `parse_out.py`, which know nothing about MCP -- this
module is the protocol surface and the docstrings the model reads, and nothing else.

Never write to stdout from this process: stdout is the JSON-RPC stream, and printing into it
is the single most common way to break a stdio MCP server. Diagnostics go to stderr.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from casino_mcp import __version__, runtime, settings

# The version travels in the initialize handshake, so a client reporting a misbehaving
# server names a release; it is the one in __init__.py, never a second copy.
server = MCPServer(
    'casino',
    instructions='Run and monitor Fortran CASINO calculations. One directory is one calculation.',
    version=__version__,
)


@server.tool()
def casino_run(
    workdir: str,
    nproc: int = settings.NPROC,
    version: str = settings.VERSION,
    restart: bool = False,
    resume: bool = False,
    unlock: bool = False,
) -> dict[str, Any]:
    """Start a CASINO calculation in workdir and return immediately.

    The runtype (vmc, vmc_opt, vmc_dmc, ...) comes from the `input` file in workdir;
    this tool only decides how the binary is launched. The calculation keeps running
    after the call returns and after this server is restarted.

    A directory that already holds an `out` is refused, because runqmc appends to it and
    the result is two runs in one file. `restart` and `resume` are the two ways past that,
    and they are opposites -- pass one.

    workdir: directory holding `input` and the wave function files.
    nproc: number of MPI processes (`vmc_nstep` in `input` is the total over all of them).
    version: binary flavour, 'opt' or 'debug'.
    restart: delete `out` and everything else the earlier run left -- `.hist` files, configs,
        optimisation output -- and start the calculation over. Inputs are kept. Destructive:
        `config.in` goes too, so what could have been continued no longer can be. Refused on a
        directory whose `input` haltqmc has set up to continue (NEWRUN : F), because CASINO
        then wants the `config.in` this would delete; casino_stop keeps a copy of the input as
        it was, and the reply to the stop says where.
    resume: carry the interrupted run on, keeping the work already done. Which of CASINO's
        two continuation routes that takes is read out of `out`, not chosen here: a run that
        CASINO stopped on max_cpu_time / max_real_time is continued by `runqmc --continue`,
        and a run that casino_stop halted is continued by a plain `runqmc` over the `input`
        that `haltqmc -u` rewrote. The reply says which one under `resume`. A run that
        reached its own end is refused -- there is nothing to continue.
    unlock: clear a stale .runqmc.lock left by a runqmc instance that died.
    """
    return runtime.start(workdir, nproc=nproc, version=version, restart=restart, resume=resume, unlock=unlock)


@server.tool()
def casino_status(job_id: str) -> dict[str, Any]:
    """State of one job: running / finished / failed / stopped, pid, runtime in seconds, exit code."""
    return runtime.status(job_id)


@server.tool()
def casino_list_jobs(limit: int = 20) -> dict[str, Any]:
    """Every known job, newest first, with its current state."""
    return runtime.listing(limit)


@server.tool()
def casino_stop(job_id: str, timeout: float = settings.STOP_TIMEOUT) -> dict[str, Any]:
    """Stop a running calculation and leave its directory ready to be continued.

    SIGTERM goes to this job's `casino` processes, as `haltqmc -k` does for the whole
    account, so `runqmc` stays alive to finish writing `out`. CASINO has no graceful-halt
    signal: the blocks it had finished stay in `out`, `vmc.hist` and `dmc.hist`, the
    current block is lost. Then `haltqmc -f -u` tidies the directory -- config.out to
    config.in, the lock file, and `input` rewritten for the work that is left -- so
    casino_run(workdir, resume=true) carries this calculation on.

    timeout: seconds the job gets to end on its own before the process group is killed.
    """
    return runtime.stop(job_id, timeout=timeout)


# Deliberately no tool yet that reads physics out of `out`: `parse_out` is a library function
# and `casino-mcp parse` exposes it, but `casino_results(job_id)` is the next stage, not this one.


def main() -> None:
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
