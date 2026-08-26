"""The `casino-mcp` command: the same runtime a shell can drive, plus the settings diagnostic.

`casino-mcp serve` is what belongs in `.mcp.json`. The rest exists because a control plane
whose state can only be inspected through the model that started it is not debuggable:

    casino-mcp config                  which CASINO the server will use, and from which variable
    casino-mcp jobs                    the registry, newest first
    casino-mcp run <dir> -p 4          start a calculation
    casino-mcp status <job_id>
    casino-mcp stop <job_id>
    casino-mcp parse <dir-or-out>      an `out` file as JSON
"""

import argparse
import json
import sys

from casino_mcp import __version__, parse_out, runtime, settings


def emit(data) -> int:
    """JSON to stdout; an `error` key is also an exit code, so a shell can branch on it."""
    print(json.dumps(data, indent=2))
    return 1 if isinstance(data, dict) and 'error' in data else 0


def cmd_serve(args) -> int:
    from casino_mcp.server import main as serve  # imported late: the MCP SDK is not needed by the rest

    serve()
    return 0


def cmd_config(args) -> int:
    """What the server would use right now. The first thing to run when a tool call refuses."""
    runqmc = runtime.find_runqmc()
    haltqmc = runtime.find_haltqmc()
    resolved = settings.resolved()
    if not any(resolved['environment'].values()):
        print('# no CASINO_* variable is set; these are the defaults', file=sys.stderr)
    if runqmc is None:
        print('# runqmc not found: set CASINO_HOME or CASINO_RUNQMC, or put it on PATH', file=sys.stderr)
    if haltqmc is None:
        print('# haltqmc not found: a job can be stopped, but its directory will not be tidied', file=sys.stderr)
    return emit({**resolved, 'runqmc': runqmc, 'haltqmc': haltqmc})


def cmd_run(args) -> int:
    return emit(
        runtime.start(
            args.workdir,
            nproc=args.nproc,
            version=args.version,
            restart=args.restart,
            resume=args.resume,
            unlock=args.unlock,
        )
    )


def cmd_status(args) -> int:
    return emit(runtime.status(args.job_id))


def cmd_jobs(args) -> int:
    return emit(runtime.listing(args.limit))


def cmd_stop(args) -> int:
    return emit(runtime.stop(args.job_id, timeout=args.timeout))


def cmd_parse(args) -> int:
    try:
        return emit(parse_out.parse_out(args.path))
    except OSError as e:
        return emit({'error': f'cannot read the `out` file: {e}'})


def build_parser() -> argparse.ArgumentParser:
    epilog = 'environment:\n' + '\n'.join(f'  {name:<22} {description}' for name, description in settings.ENVIRONMENT)
    parser = argparse.ArgumentParser(
        prog='casino-mcp',
        description='An MCP control plane over the Fortran CASINO code.',
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--version', action='version', version=f'casino-mcp {__version__}')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('serve', help='run the MCP server on stdio').set_defaults(func=cmd_serve)
    sub.add_parser('config', help='print the CASINO installation the server will use').set_defaults(func=cmd_config)

    run = sub.add_parser('run', help='start a calculation')
    run.add_argument('workdir')
    run.add_argument('-p', '--nproc', type=int, default=settings.NPROC, help='MPI processes (default: %(default)s)')
    run.add_argument('--version', dest='version', default=settings.VERSION, help='binary flavour (default: %(default)s)')
    run.add_argument('--restart', action='store_true', help='delete `out` and the rest of what an earlier run left, then start over')
    # `--continue` is the name runqmc uses; `continue` cannot be a Python identifier, so the
    # argument this sets, and the tool parameter, are both `resume`.
    run.add_argument(
        '--resume',
        '--continue',
        dest='resume',
        action='store_true',
        help='carry on an interrupted run: `runqmc --continue`, or a plain runqmc over the input haltqmc updated',
    )
    run.add_argument('--unlock', action='store_true', help='clear a stale .runqmc.lock')
    run.set_defaults(func=cmd_run)

    status = sub.add_parser('status', help='state of one job')
    status.add_argument('job_id')
    status.set_defaults(func=cmd_status)

    listing = sub.add_parser('jobs', help='every known job, newest first')
    listing.add_argument('-n', '--limit', type=int, default=20)
    listing.set_defaults(func=cmd_jobs)

    stop = sub.add_parser('stop', help='stop a running job and hand its directory to haltqmc')
    stop.add_argument('job_id')
    stop.add_argument('--timeout', type=float, default=settings.STOP_TIMEOUT, help='seconds before the group is killed (default: %(default)s)')
    stop.set_defaults(func=cmd_stop)

    parse = sub.add_parser('parse', help='a CASINO `out` file as JSON')
    parse.add_argument('path', help='a calculation directory or an `out` file')
    parse.set_defaults(func=cmd_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
