"""MCP server driving the Fortran CASINO code.

Tools are named CASINO operations; there is deliberately no generic shell tool. Everything
these tools do lives in `runtime.py` and `parse_out.py`, which know nothing about MCP -- this
module is the protocol surface and the docstrings the model reads, and nothing else.

Never write to stdout from this process: stdout is the JSON-RPC stream, and printing into it
is the single most common way to break a stdio MCP server. Diagnostics go to stderr.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from casino_mcp import runtime, settings

server = MCPServer('casino', instructions='Run and monitor Fortran CASINO calculations. One directory is one calculation.')


@server.tool()
def casino_run(
    workdir: str,
    nproc: int = settings.NPROC,
    version: str = settings.VERSION,
    overwrite: bool = False,
    unlock: bool = False,
) -> dict[str, Any]:
    """Start a CASINO calculation in workdir and return immediately.

    The runtype (vmc, vmc_opt, vmc_dmc, ...) comes from the `input` file in workdir;
    this tool only decides how the binary is launched. The calculation keeps running
    after the call returns and after this server is restarted.

    workdir: directory holding `input` and the wave function files.
    nproc: number of MPI processes (`vmc_nstep` in `input` is the total over all of them).
    version: binary flavour, 'opt' or 'debug'.
    overwrite: allow running in a directory that already holds an `out` file.
    unlock: clear a stale .runqmc.lock left by a runqmc instance that died.
    """
    return runtime.start(workdir, nproc=nproc, version=version, overwrite=overwrite, unlock=unlock)


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
    """Stop a running calculation.

    Signals the whole process tree (launcher, runqmc, mpirun, casino). CASINO has no
    graceful-halt signal, so whatever blocks it had finished stay in `out`, `vmc.hist`
    and `dmc.hist`, and the current block is lost. The stale lock file is cleared.

    timeout: seconds to wait after SIGTERM before SIGKILL.
    """
    return runtime.stop(job_id, timeout=timeout)


# Deliberately no tool yet that reads physics out of `out`: `parse_out` is a library function
# and `casino-mcp parse` exposes it, but `casino_results(job_id)` is the next stage, not this one.


def main() -> None:
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
