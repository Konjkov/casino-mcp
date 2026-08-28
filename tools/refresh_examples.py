"""Re-run every reproducible calculation in `examples/` and report what changed.

    python tools/refresh_examples.py                  # run and report, touching nothing
    python tools/refresh_examples.py --write          # adopt the new `out` files
    python tools/refresh_examples.py --only stowfn    # just the ones whose path matches

About ten minutes for the tree on four processes, which is what the committed files were made
with -- keep `--nproc` there, because a serial CASINO prints no per-process quantities and
comparing one against them reports lost fields that are nothing but the process count.

This is the operation the committed tree exists for. Every example fixes `random_seed`, which
makes a plain VMC run reproduce its `out` to the last digit against the same binary -- but not
every run: an optimisation redistributes configurations across MPI processes, and the order it
does that in is not the seed's to fix, so `backflow/3_1_1/25` lands on a different energy every
time. Values are therefore reported and never trusted. Two kinds of movement are separated:

  * a **shape** change -- a phase, a field or a keyword that used to be parsed and no longer
    is -- means CASINO's output format changed and `parse_out` has quietly stopped reading
    something. That is the failure `tests/integration/test_examples_rerun.py` asserts on.
  * a **value** change means the physics or the random-number stream moved. It is not
    necessarily wrong: a new CASINO release may legitimately produce different numbers.

Nothing runs in `examples/` itself: each calculation is copied to a scratch directory first,
because `runqmc` appends to an existing `out` rather than replacing it, and because the
committed files are the reference everything else is validated against.

An interrupted run cannot be reproduced by running -- there is no keyword for "stop after four
cycles" -- so the two incomplete examples are skipped and keep the `out` they were committed
with.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from casino_mcp.parse_out import parse_out  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
INPUTS = ('input', '*.data', '*.casl')


def payload(directory):
    """What a calculation needs to start: its input, wavefunction, pseudopotential, parameters."""
    files = []
    for pattern in INPUTS:
        files += [p for p in sorted(directory.glob(pattern)) if p.is_file() and not p.name.startswith('config')]
    return files


def fields(parsed):
    """Every path in the parse that carries a number, so a lost field can be named.

    Two things are left out, both because whether CASINO prints them is a property of the run
    rather than of the output format, so they flicker between two runs of the same binary:

    - `efficiency`, which is computed from a measured time and cannot be printed for a block
      short enough to take 0.00 s;
    - the VMC `reblock` dump, which `vmc.f90` prints only inside the `derr > 0.1*err` branch --
      that is, only when the reblocking did not converge, which depends on the sample. (The DMC
      one is unconditional, but the exclusion is by path and covers both; a DMC run that stopped
      printing it would have to be caught by `test_parse_out.py` instead.)
    """
    found = set()

    def walk(node, path):
        if isinstance(node, dict):
            if 'value' in node:
                if node['value'] is not None:
                    found.add(path)
                return
            for key, child in node.items():
                walk(child, f'{path}.{key}' if path else key)
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f'{path}[{i}]')

    walk(parsed, '')
    return {path for path in found if not path.endswith('efficiency') and '.reblock.' not in path}


def shape(parsed):
    """What the parse looks like, with the numbers taken out."""
    return {
        'phases': [phase['kind'] for phase in parsed['phases']],
        'keywords': set(parsed['keywords']),
        'fields': fields(parsed),
        'complete': parsed['complete'],
    }


def run(directory, nproc):
    """Run the calculation in a scratch copy and return (out_text, seconds, error)."""
    with tempfile.TemporaryDirectory(prefix='casino-example-') as scratch:
        work = Path(scratch)
        for source in payload(directory):
            shutil.copyfile(source, work / source.name)
        started = time.monotonic()
        finished = subprocess.run(['runqmc', '-p', str(nproc)], cwd=work, capture_output=True, text=True, check=False)
        elapsed = time.monotonic() - started
        out = work / 'out'
        if finished.returncode != 0 or not out.is_file():
            tail = (finished.stdout + finished.stderr).strip().split('\n')
            return None, elapsed, ' / '.join(line.strip() for line in tail[-3:])
        return out.read_text(), elapsed, ''


def energy(parsed):
    """The energy `result` points at, or None if no phase reported one."""
    return parsed['result'].get('energy', parsed['result']).get('value')


def compare(old, new):
    """The differences worth printing, shape first."""
    before, after = shape(old), shape(new)
    notes = []
    if before['phases'] != after['phases']:
        notes.append(f'SHAPE phases {before["phases"]} -> {after["phases"]}')
    for name in ('keywords', 'fields'):
        lost = sorted(before[name] - after[name])
        if lost:
            notes.append(f'SHAPE {name} no longer parsed: {", ".join(lost[:6])}{" ..." if len(lost) > 6 else ""}')
    if energy(old) != energy(new):
        notes.append(f'value energy {energy(old)} -> {energy(new)}')
    return notes


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--write', action='store_true', help='copy each new `out` over the committed one')
    # 4 is what the committed `out` files were made with, and it has to match: a serial run
    # prints no per-process quantities, so comparing one against them loses fields for no reason
    parser.add_argument('--nproc', type=int, default=4, help='MPI processes per calculation (default 4, as the tree was made)')
    parser.add_argument('--only', default='', help='only calculations whose path contains this')
    args = parser.parse_args()

    total, failures = 0.0, 0
    for directory in sorted(p.parent for p in EXAMPLES.rglob('out')):
        name = str(directory.relative_to(EXAMPLES))
        if args.only and args.only not in name:
            continue
        old = parse_out(directory)
        if not old['complete']:
            print(f'{name:56} skipped: an interrupted run cannot be reproduced')
            continue

        text, elapsed, error = run(directory, args.nproc)
        total += elapsed
        if error:
            failures += 1
            print(f'{name:56} FAILED after {elapsed:5.1f}s: {error}')
            continue

        if args.write:
            (directory / 'out').write_text(text)
            new = parse_out(directory)
        else:
            with tempfile.TemporaryDirectory() as holding:
                held = Path(holding) / 'out'
                held.write_text(text)
                new = parse_out(held)

        notes = compare(old, new)
        print(f'{name:56} {elapsed:5.1f}s  cpu {new["cpu_time"]["value"]}')
        for note in notes:
            print(f'{"":58}{note}')

    print(f'\n{total:.0f}s in total, {failures} failed')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
