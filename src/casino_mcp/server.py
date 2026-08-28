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
def casino_prepare(
    source: str,
    dest: str,
    runtype: str = '',
    overrides: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Copy a calculation into a new directory and write the `input` for the next run in it.

    This is how a calculation becomes the next one: optimise a wave function, then prepare a
    `vmc_dmc` directory beside it; halve the timestep into another; change the target weight
    into a third. A copy rather than an edit, because a number whose input was overwritten in
    place can no longer be reproduced, and because casino_run refuses a directory that already
    holds a run -- rightly.

    What is copied is what a calculation is given and never what a run produced: `input`, the
    orbital file, `correlation.data`, `parameters.casl`, the pseudopotentials, and `config.in`
    (which a dmc-only or `opt` runtype reads as an input). Not `out`, not the `.hist` files, not
    `config.out`. A symlinked orbital file is copied by content, so the new directory stands on
    its own.

    runtype: the runtype the new directory is for -- vmc, vmc_opt, opt, vmc_dmc, vmc_dmc_equil,
        dmc_dmc, dmc_equil, dmc_stats. Every keyword that runtype needs and the source input
        does not set is filled from a working default; every keyword the source does set is
        kept, so the electron count, the basis and any hand tuning survive. Leave it empty to
        keep the source's runtype and only apply `overrides`.
    overrides: keywords to set, as {name: value}, and they win over both the source and the
        defaults. A null value deletes the keyword; a value containing newlines is written as a
        `%block` (that is how `opt_plan` and `npcell` are set). Values are written verbatim, so
        booleans are 'T' and 'F' as CASINO spells them.

    Nothing is written unless the result would actually run: the keyword combinations CASINO
    only rejects at run time are checked first (an optimisation sample smaller than the DMC
    target weight, `opt_backflow` without `backflow`, a missing mandatory keyword), and so is
    the presence of every file the input tells CASINO to read. A refusal names the problems and
    creates no directory. What is legal but probably unintended -- a `dtdmc` still at CASINO's
    placeholder default, `dmc_stats_nstep` not divisible by its block count, keywords left over
    from the runtype this was copied from -- comes back in `warnings` and does not stop it.
    """
    return runtime.prepare(source, dest, runtype=runtype, overrides=overrides)


@server.tool()
def casino_status(job_id: str) -> dict[str, Any]:
    """State of one job: running / finished / failed / stopped, pid, runtime in seconds, exit code."""
    return runtime.status(job_id)


@server.tool()
def casino_results(job_id: str) -> dict[str, Any]:
    """Physics out of a job's files: energies, error bars, variance, per-block numbers.

    Reads `out` and returns it as phases, because a CASINO run is a sequence of them and not
    one result: `vmc_opt` writes a VMC and an optimization phase per cycle, `vmc_dmc` writes
    VMC, DMC equilibration and DMC statistics accumulation. `result` points at the number that
    is this run's answer, and every value carries the file and line it was read from. Nothing
    is computed here that CASINO did not print, and a value it did not print comes back as null
    with the reason.

    A DMC run that has not ended is readable too, and this is the only way to read one: CASINO
    writes the mixed estimators into `out` at the very end, and until then the current estimate
    lives in `dmc.status`, which it rewrites after every statistics block and deletes when the
    run finishes. When that file is there it is parsed into `dmc_status` and `result` points at
    it, so a running job answers with the estimate as of its last block -- and never with the
    VMC energy of the configuration-generation phase, which is the trial wave function's and not
    the calculation's. A run stopped by casino_stop keeps its `dmc.status`, so the last estimate
    it reached survives the stop.

    While the run is still equilibrating there is no DMC energy anywhere yet, and `result` then
    says so rather than answering with an earlier phase.
    """
    return runtime.results(job_id)


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


def main() -> None:
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
