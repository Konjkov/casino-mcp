"""The `casino-mcp` command: the same runtime a shell can drive, plus the settings diagnostic.

`casino-mcp serve` is what belongs in `.mcp.json`. The rest exists because a control plane
whose state can only be inspected through the model that started it is not debuggable:

    casino-mcp config                  which CASINO the server will use, and from which variable
    casino-mcp jobs                    the registry, newest first
    casino-mcp run <dir> -p 4          start a calculation
    casino-mcp status <job_id|dir>     every job argument takes a directory too
    casino-mcp wait <job_id|dir>       block until the calculation ends
    casino-mcp stop <job_id|dir>
    casino-mcp results <job_id|dir>    the physics of one job, live runs included
    casino-mcp results <dir> -f vmc.efficiency   ... or just the numbers asked for
    casino-mcp prepare <src> <dst> --runtype vmc_dmc -s dtdmc=0.005
    casino-mcp prepare <src> <dst> --runtype vmc_opt --jastrow u,chi,f
    casino-mcp prepare <src> <dst> --geminal p:2,d:1 -g anchors=1
    casino-mcp input <job_id|dir>      the keywords a calculation was given, `random_seed` included
    casino-mcp parse <dir-or-out>      an `out` file as JSON
"""

import argparse
import json
import sys

from casino_mcp import __version__, correlation_data, geminal, input_file, parse_out, runtime, settings


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


def cmd_wait(args) -> int:
    return emit(runtime.wait(args.job_id, timeout=args.timeout))


def keyword_pairs(settings_list, flag: str = '--set') -> dict:
    """`-s name=value` pairs off the command line; an empty value deletes the keyword."""
    pairs = {}
    for item in settings_list or ():
        name, separator, value = item.partition('=')
        if not separator:
            raise SystemExit(f'{flag} expects name=value, got {item!r}')
        pairs[name.strip()] = value if value != '' else None
    return pairs


def term_names(value: str | None, default: tuple[str, ...]) -> list[str] | None:
    """`--jastrow u,chi,f`, and a bare `--jastrow` for all of them."""
    if value is None:
        return None
    terms = [term.strip().lower() for term in value.split(',') if term.strip()]
    return terms or list(default)


def jastrow_numbers(settings_list) -> dict:
    """`-j name=value` pairs; every Jastrow setting is a number, unlike the input keywords."""
    pairs = {}
    for name, value in keyword_pairs(settings_list, '--jastrow-set').items():
        number = input_file.number(value)
        if number is None:
            raise SystemExit(f'--jastrow-set expects a number, got {name}={value!r}')
        pairs[name] = number if name.startswith('cutoff') else int(number)
    return pairs


def geminal_values(settings_list) -> dict:
    """`-g seed=-0.19` pairs; `anchors` is a list of orbitals and everything else is a number."""
    values = {}
    for name, value in keyword_pairs(settings_list, '--geminal-set').items():
        if name == 'anchors':
            orbitals = [orbital.strip() for orbital in (value or '').split(',') if orbital.strip()]
            if not all(orbital.isdigit() for orbital in orbitals):
                raise SystemExit(f'--geminal-set anchors expects orbital numbers, got {value!r}')
            values[name] = [int(orbital) for orbital in orbitals]
            continue
        number = input_file.number(value)
        if number is None:
            raise SystemExit(f'--geminal-set expects a number, got {name}={value!r}')
        values[name] = number if name in ('seed', 'seed2', 'purity') else int(number)
    return values


def cmd_prepare(args) -> int:
    return emit(
        runtime.prepare(
            args.source,
            args.dest,
            runtype=args.runtype,
            overrides=keyword_pairs(args.set),
            jastrow=term_names(args.jastrow, correlation_data.TERMS),
            backflow=term_names(args.backflow, correlation_data.BACKFLOW_TERMS),
            jastrow_settings=jastrow_numbers(args.jastrow_set),
            geminal=term_names(args.geminal, ()),
            geminal_settings=geminal_values(args.geminal_set),
        )
    )


def cmd_results(args) -> int:
    fields = [field for item in args.field or () for field in item.split(',') if field]
    return emit(runtime.results(args.job_id, fields=fields or None))


def cmd_input(args) -> int:
    return emit(runtime.calculation_input(args.job_id))


def cmd_jobs(args) -> int:
    return emit(runtime.listing(args.limit, workdir=args.workdir or ''))


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
    status.add_argument('job_id', help='a job id, or the calculation directory: then the newest job that ran there')
    status.set_defaults(func=cmd_status)

    wait = sub.add_parser('wait', help='block until a running calculation ends')
    wait.add_argument('job_id', help='a job id, or the calculation directory')
    wait.add_argument('--timeout', type=float, default=settings.WAIT_TIMEOUT, help='seconds to wait (default: %(default)s)')
    wait.set_defaults(func=cmd_wait)

    results = sub.add_parser('results', help="what one job's files say: the parsed `out`, and dmc.status if the run is still going")
    results.add_argument('job_id', help='a job id, or the calculation directory')
    results.add_argument(
        '-f',
        '--field',
        action='append',
        metavar='PATH',
        help='answer with just this path; repeatable, and comma-separated lists are split. '
        'vmc/opt/dmc_equil/dmc_stats are the last phase of that kind, opt[3] the cycle CASINO numbered, '
        'phases[-1] by position, anything else a key of the run (keywords.DTVMC, vmc.efficiency, result.energy)',
    )
    results.set_defaults(func=cmd_results)

    calc_input = sub.add_parser('input', help='the keywords and blocks a calculation was given')
    calc_input.add_argument('job_id', help='a job id, or a calculation directory -- including one that has never been run')
    calc_input.set_defaults(func=cmd_input)

    prepare = sub.add_parser('prepare', help='copy a calculation into a new directory and write the input for the next run')
    prepare.add_argument('source', help='the calculation to start from')
    prepare.add_argument('dest', help='the new directory; it must not already hold a calculation')
    prepare.add_argument('--runtype', default='', help=f'fill in what this runtype needs ({", ".join(sorted(input_file.RECIPES))})')
    prepare.add_argument(
        '-s',
        '--set',
        action='append',
        metavar='NAME=VALUE',
        help='set one keyword; repeatable. An empty value (NAME=) deletes it.',
    )
    prepare.add_argument(
        '--jastrow',
        nargs='?',
        const='',
        metavar='TERMS',
        help=f'write a blank Jastrow factor: the terms, comma-separated ({",".join(correlation_data.TERMS)}), or bare for all of them',
    )
    prepare.add_argument(
        '--backflow',
        nargs='?',
        const='',
        metavar='TERMS',
        help=f'write a blank backflow function too: the terms ({",".join(correlation_data.BACKFLOW_TERMS)}), or bare for all of them',
    )
    prepare.add_argument(
        '-j',
        '--jastrow-set',
        action='append',
        metavar='NAME=VALUE',
        help=f'one Jastrow or backflow setting; repeatable ({", ".join(sorted(correlation_data.DEFAULTS))})',
    )
    prepare.add_argument(
        '--geminal',
        nargs='?',
        const='',
        metavar='CHANNELS',
        help=f'write the GEMINAL block of a parameters.casl: the channels to correlate ({",".join(sorted(geminal.CHANNELS))} as l:n, '
        f'e.g. p:2,d:1), or bare for the Hartree-Fock geminal alone',
    )
    prepare.add_argument(
        '-g',
        '--geminal-set',
        action='append',
        metavar='NAME=VALUE',
        help=f'one geminal setting; repeatable ({", ".join(sorted(geminal.DEFAULTS))}). anchors takes a list: anchors=1,2',
    )
    prepare.set_defaults(func=cmd_prepare)

    listing = sub.add_parser('jobs', help='every known job, newest first')
    listing.add_argument('-n', '--limit', type=int, default=20)
    listing.add_argument('-C', '--workdir', help='only the jobs that ran in this directory')
    listing.set_defaults(func=cmd_jobs)

    stop = sub.add_parser('stop', help='stop a running job and hand its directory to haltqmc')
    stop.add_argument('job_id', help='a job id, or the calculation directory')
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
