"""Writing the GEMINAL block of a `parameters.casl` for CASINO.

Text out, no MCP, and the third writer of the same shape as `input_file` and
`correlation_data`: the caller says what the wave function should contain, this returns the
file that asks CASINO for it. `psi_s : geminal` replaces the Slater determinant by a sum of
geminal determinants,

    Psi_S(R) = sum_n c_n det M^(n),    Phi_n(r,r') = sum_mk g^(n)_mk phi_m(r) phi_k(r'),

and every c_n and g^(n)_mk of it lives in this one block. There is no CASINO utility that
writes it, and unlike a Jastrow factor it cannot be started from zeros: a pairing matrix with
nothing on its diagonal is a singular M, so the file has to name the orbitals the geminal
pairs before the first run rather than after the first optimisation.

**CASL is not YAML.** A constraint line reads `2^g_5,5=2^g_4,4`, which is a bare scalar no
YAML parser accepts, so the block is generated as plain text and never through a YAML dumper.
CASL's own reader strips `#` comments (`casl.f90`, `read_casl_file`), which is what makes the
provenance line at the top of the file safe.

What this knows that a text editor does not is which orbitals belong together. A correlating
geminal built out of one component of a degenerate level -- one of the three 2p orbitals, say
-- is not spherically symmetric, and optimizing it breaks the symmetry of the state it is
meant to describe. So the orbitals of a level have to be tied to each other, component by
component, and that is what the Constraints block is for. The levels are derived from the
orbital file: each MO is classified by the (l, m-slot) its coefficients live on, MOs of the
same l are grouped into levels of 2l+1 in file order, and a level whose components are not
one clean m-slot each is demoted to a diagonal-only tie rather than guessed at -- a
component-wise off-diagonal tie between two levels that are mixed differently is a constraint
between things that are not each other's counterparts.

    shells        levels this can tie component by component: the whole 2l+1 in one group,
                  each ordered by m-slot with slot 0 last, and the last member is the one
                  that carries the value in Parameters (the others are `determined`)
    diag_shells   levels that are not rotationally closed -- D2h-mixed a_g pairs and the
                  like -- tied on the diagonal only

Three geminals at most, and each of them is one decision:

    Geminal 1   the Hartree-Fock determinant: g_m,m = 1 over the doubly occupied MOs and
                u_m,k = 1 over the singly occupied ones. On its own it is the Slater
                determinant exactly, which is the check the manual recommends
    Geminal 2   the correlating one: the anchors it keeps from Geminal 1 at 1, and the
                shells it correlates, optimizable
    Geminal 3   the mirror, written only when asked for: c = -1, no anchors, and every g of
                it tied to Geminal 2's by the constraints, so the two hold the same numbers
                and differ in sign and in the anchors alone

The unpaired columns are not optional. A geminal matrix is N_up x N_up, its last
N_up - N_down columns hold one orbital each, and `check_umat` errstops on any geminal with a
non-zero c whose unpaired column is empty -- so every geminal written here gets its `u`
lines, not just the first. They are fixed by CASINO's own rule (`parse_umat_el` refuses an
optimizable one), and the orbitals they name are the singly occupied ones of the reference
determinant.
"""

import re
from math import factorial, sqrt
from pathlib import Path

# The channels a caller can ask for, as `p:2` -- the first two p levels -- and the angular
# momentum each names. Nothing above g: a gwfn.data shell code stops there too.
CHANNELS = {'s': 0, 'p': 1, 'd': 2, 'f': 3, 'g': 4}

# `Code for shell types (s/sp/p/d/f... 1/2/3/4/5...)` in gwfn.data, and the angular momenta of
# the basis functions each code contributes, in the order they appear. Code 2 is the sp shell
# CRYSTAL writes: one s function and a p triple, four functions and not three, which is why
# this is a table of functions rather than a table of l.
SHELL_CODE = {1: (0,), 2: (0, 1), 3: (1,), 4: (2,), 5: (3,), 6: (4,)}

# CASINO evaluates its real solid harmonics with the constant factors of the d functions --
# the 3 in 3xy and the like -- left out, and the converters multiply them into the orbital
# coefficients instead. `molden2qmc.py:d_normalize` states it and calls it "one historical
# CASINO inconsistency which may be easily overlooked": it is done for d and *not* for f and
# g. The m-dependent factor beside it, sqrt((2 - delta_m0)(l-|m|)!/(l+|m|)!), is applied to
# all of d, f and g. Both are divided out before the components of a level are compared,
# because a comparison of weights on differently scaled functions compares the scaling.
D_PREMULTIPLIED = (0.5, 3.0, 3.0, 3.0, 6.0)

DEFAULTS = {
    # The two leading correlating channels start displaced from zero because a geminal that
    # holds only its anchors has a singular pairing matrix and contributes nothing: the
    # optimisation would open on a wave function it cannot tell from the Hartree-Fock one.
    # These are the values the geminal examples in this repository start their cycles from;
    # -0.19 is what the Be 2s-2p near-degeneracy wants instead of -0.05.
    'seed': -0.05,
    'seed2': -0.02,
    'mirror': 0,  # write Geminal 3: c = -1, tied to Geminal 2 parameter for parameter
    # The share of an MO's weight that has to sit on one m-slot before the MO counts as that
    # component and its level as rotationally closed. A pure atomic orbital is 1.0 to within
    # printing precision; a symmetry-broken molecular one is well under this.
    'purity': 0.98,
    # The occupied MOs Geminal 2 keeps at 1 alongside the shells it correlates. Derived --
    # every occupied MO no correlated shell contains -- unless given: Be's 2s pair is
    # *replaced* by the 2p block rather than kept beside it, and only the caller knows that.
    'anchors': None,
}

# Where the geometry and the orbitals are. `stowfn.data` has orbital coefficients of its own
# but lays them out by Slater exponent rather than by shell, and the rest are written by
# periodic codes. A channel-less geminal -- the Hartree-Fock one -- needs no orbital file at
# all and works for any basis, which is why this table is consulted only when channels are.
READABLE_BASIS = 'gaussian'

HEADER = re.compile(r'\s*(number of electrons|number of shells|number of basis functions|code for shell types|periodicity)', re.IGNORECASE)
COEFFICIENTS = 'ORBITAL COEFFICIENTS'
UNRESTRICTED = re.compile(r'\s*spin unrestricted\s*:?\s*$', re.IGNORECASE)

# A Fortran real as every gwfn.data writes them. Read by pattern and not by column, because
# the block is written in fixed-width fields with no separator between them -- a negative
# number follows the previous one with no space at all -- and `split()` would join the two.
REAL = re.compile(r'[-+]?\d*\.\d+[EeDd][-+]?\d+')


def parse_channels(names) -> tuple[list[tuple[int, int]], list[str]]:
    """`['p:2', 'd:1']` -> [(1, 2), (2, 1)], and what could not be read that way."""
    channels, errors = [], []
    for name in names:
        letter, separator, count = str(name).strip().lower().partition(':')
        if letter not in CHANNELS:
            errors.append(f'no such channel: {name!r}. Write them as l:n -- {", ".join(sorted(CHANNELS))} -- e.g. p:2 for the first two p levels')
            continue
        if not separator:
            count = '1'
        if not count.strip().isdigit() or int(count) < 1:
            errors.append(f'{name!r}: the count after the colon must be a positive whole number of levels')
            continue
        channels.append((CHANNELS[letter], int(count)))
    return channels, errors


def channel_name(l: int) -> str:
    return next(name for name, value in CHANNELS.items() if value == l)


def basis_functions(codes: list[int]) -> list[tuple[int, int]]:
    """The (l, m-slot) of every basis function, in the order the file writes their coefficients."""
    functions = []
    for code in codes:
        if code not in SHELL_CODE:
            raise ValueError(f'shell type code {code} is not one of {", ".join(str(known) for known in sorted(SHELL_CODE))} (s/sp/p/d/f/g)')
        for l in SHELL_CODE[code]:
            functions.extend((l, slot) for slot in range(2 * l + 1))
    return functions


def normalization(l: int, slot: int) -> float:
    """The constant folded into the coefficient of this function, which is divided back out.

    Slots run 0, +1, -1, +2, -2, ... so |m| is (slot + 1) // 2. Below d there is nothing to
    remove: `m_dependent_factor` is 1 for l < 2 and for m = 0, and only d carries the
    premultiplied solid-harmonic constants.
    """
    if l < 2:
        return 1.0
    m = (slot + 1) // 2
    factor = 1.0 if m == 0 else sqrt(2.0 * factorial(l - m) / factorial(l + m))
    return factor * (D_PREMULTIPLIED[slot] if l == 2 else 1.0)


def tokens_after(lines: list[str], start: int, count: int, what: str) -> list[str]:
    """The next `count` whitespace-separated tokens after line `start`, over as many lines as it takes.

    Whitespace-separated, unlike the orbital coefficients: everything in the header of a
    gwfn.data is written wide enough to keep its spaces, and it is only the coefficient block
    that packs its fields together. What the token means is the caller's business -- these
    are integers everywhere but the spin-unrestricted flag, which is a Fortran logical.
    """
    tokens: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped and set(stripped) == {'-'}:
            continue  # the rule under a section heading
        tokens.extend(line.split())
        if len(tokens) >= count:
            return tokens[:count]
    raise ValueError(f'{what}: found {len(tokens)} of the {count} values the file says are there')


def read_orbitals(path) -> dict:
    """The orbitals of a gwfn.data: how many, on what functions, with which coefficients.

    Only what the level classification needs: the shell types, which say what function each
    coefficient belongs to, and the coefficients themselves. A spin-unrestricted file holds
    the up-spin orbitals and then the down-spin ones, both N_bf x N_bf, and both are read --
    but the levels are taken off the up-spin set alone, which `describe` says out loud and
    `spin_check` is there to question.
    """
    path = Path(path)
    lines = path.read_text(errors='replace').splitlines()
    marks: dict[str, int] = {}
    for i, line in enumerate(lines):
        found = HEADER.match(line)
        if found:
            marks.setdefault(found.group(1).lower().replace(' ', '_'), i)
        if UNRESTRICTED.match(line):
            marks.setdefault('unrestricted', i)
        if line.strip() == COEFFICIENTS:
            marks.setdefault('coefficients', i)
    for name in ('number_of_shells', 'number_of_basis_functions', 'code_for_shell_types', 'coefficients'):
        if name not in marks:
            raise ValueError(f'{path} has no "{name.replace("_", " ")}" section: this is not a gwfn.data the orbitals can be read out of')

    periodicity = int(tokens_after(lines, marks['periodicity'], 1, f'{path}: periodicity')[0]) if 'periodicity' in marks else 0
    electrons = int(tokens_after(lines, marks['number_of_electrons'], 1, f'{path}: number of electrons')[0]) if 'number_of_electrons' in marks else 0
    shells = int(tokens_after(lines, marks['number_of_shells'], 1, f'{path}: number of shells')[0])
    nbf = int(tokens_after(lines, marks['number_of_basis_functions'], 1, f'{path}: number of basis functions')[0])
    codes = [int(code) for code in tokens_after(lines, marks['code_for_shell_types'], shells, f'{path}: shell type codes')]
    functions = basis_functions(codes)
    if len(functions) != nbf:
        raise ValueError(f'{path}: the {shells} shell type codes describe {len(functions)} basis functions and the file says there are {nbf}')

    unrestricted = 'unrestricted' in marks and tokens_after(lines, marks['unrestricted'], 1, f'{path}: spin unrestricted')[0].strip('.').lower() in (
        'true',
        't',
    )
    values = []
    for line in lines[marks['coefficients'] + 1 :]:
        values.extend(REAL.findall(line))
        if len(values) >= (2 if unrestricted else 1) * nbf * nbf:
            break
    if len(values) < nbf * nbf:
        raise ValueError(
            f'{path}: the ORBITAL COEFFICIENTS section holds {len(values)} numbers, and {nbf} orbitals over {nbf} functions need {nbf * nbf}'
        )

    # The scaling is divided out here and not where the weights are compared, so that
    # everything downstream of this reads a coefficient of the spherical harmonic itself.
    scale = [normalization(l, slot) for l, slot in functions]
    orbitals = []
    for spin in range(min(2 if unrestricted else 1, len(values) // (nbf * nbf))):
        block = values[spin * nbf * nbf : (spin + 1) * nbf * nbf]
        orbitals.append([[float(block[mo * nbf + i].replace('D', 'E').replace('d', 'e')) / scale[i] for i in range(nbf)] for mo in range(nbf)])
    return {
        'path': str(path),
        'periodicity': periodicity,
        'electrons': electrons,
        'norb': nbf,
        'functions': functions,
        'unrestricted': unrestricted,
        'orbitals': orbitals,  # one list of MOs per spin present in the file
    }


def classify(coefficients: list[float], functions: list[tuple[int, int]], purity: float) -> tuple[int, int, bool]:
    """One MO as (l, m-slot, whether it is that one component and nothing else).

    The l is the one carrying most of the MO's weight, and the slot the one carrying most of
    that l's. `pure` is the question the level structure hangs on: an atomic 2p_x is one slot
    to within printing precision, while a molecular orbital that mixes two components of the
    same l is not, and only the first kind can be tied to its siblings component by component.
    """
    squares = [c * c for c in coefficients]
    weight: dict[int, float] = {}
    for (l, _), c2 in zip(functions, squares, strict=True):
        weight[l] = weight.get(l, 0.0) + c2
    l = max(sorted(weight), key=lambda value: weight[value])
    slots = [0.0] * (2 * l + 1)
    for (this_l, slot), c2 in zip(functions, squares, strict=True):
        if this_l == l:
            slots[slot] += c2
    best = slots.index(max(slots))
    total = sum(slots)
    return l, best, bool(total) and slots[best] / total > purity


def mo_levels(orbitals: dict, spin: int = 0, purity: float = DEFAULTS['purity']) -> dict[int, list[tuple[list[int], bool]]]:
    """The MOs of one spin, grouped into levels of 2l+1, keyed by l and in file order.

    A level is 2l+1 consecutive MOs of the same l, which is what a spherical (or near
    spherical) system produces and what the orbital file lists in energy order. A closed one
    is reordered by m-slot with slot 0 last, because the last member of a group is the one
    that carries the value in `Parameters` and slot 0 -- the only component of a p or d level
    that has no partner to be rotated into -- is the natural one to declare.
    """
    by_l: dict[int, list[tuple[int, int, bool]]] = {}
    for index, coefficients in enumerate(orbitals['orbitals'][spin], start=1):
        l, slot, pure = classify(coefficients, orbitals['functions'], purity)
        by_l.setdefault(l, []).append((index, slot, pure))
    levels = {}
    for l, mos in sorted(by_l.items()):
        size = 2 * l + 1
        levels[l] = []
        for start in range(0, len(mos), size):
            chunk = mos[start : start + size]
            closed = len(chunk) == size and all(pure for _, _, pure in chunk) and sorted(slot for _, slot, _ in chunk) == list(range(size))
            if closed:
                chunk = sorted(chunk, key=lambda member: member[1])
                chunk = chunk[1:] + chunk[:1]  # slot 0 last: it is the one that carries the value
            levels[l].append(([index for index, _, _ in chunk], closed))
    return levels


def select(levels: dict, channels: list[tuple[int, int]]) -> tuple[list[list[int]], list[list[int]], list[str], list[str]]:
    """The levels the asked-for channels name: the closed ones, the rest, and what went wrong."""
    shells, diagonal, errors, notes = [], [], [], []
    for l, count in channels:
        available = levels.get(l, [])
        if count > len(available):
            errors.append(
                f'{channel_name(l)}:{count} was asked for and the orbital file has {len(available)} {channel_name(l)} level(s). '
                f'Available: {", ".join(f"{channel_name(other)}:{len(found)}" for other, found in sorted(levels.items())) or "none"}'
            )
            continue
        for members, closed in available[:count]:
            if closed:
                shells.append(members)
            else:
                diagonal.append(members)
                notes.append(
                    f'the {channel_name(l)} level {members} is not one clean m-component per orbital, so it is tied on the diagonal only: '
                    f"an off-diagonal tie component by component would constrain orbitals that are not each other's counterparts"
                )
    return shells, diagonal, errors, notes


def settings_for(overrides: dict | None = None) -> dict:
    """The defaults with the caller's changes folded in, and nothing invented."""
    overrides = {name.lower(): value for name, value in (overrides or {}).items()}
    unknown = sorted(name for name in overrides if name not in DEFAULTS)
    if unknown:
        raise KeyError(f'no such geminal setting: {", ".join(unknown)}. Known: {", ".join(sorted(DEFAULTS))}')
    return {**DEFAULTS, **overrides}


def value(number, optimizable: bool) -> str:
    return f'[ {float(number)}, {"optimizable" if optimizable else "fixed"} ]'


def parameter(name: str, number, optimizable: bool) -> str:
    return f'      {name}: {value(number, optimizable)}'


def unpaired_lines(unpaired: list[int]) -> list[str]:
    """One `u_m,k` per unpaired column, fixed at 1.

    Every geminal with a non-zero c needs the whole set: an empty unpaired column makes M
    singular at every configuration, which is `check_umat`'s errstop. Fixed because
    `parse_umat_el` refuses an optimizable one outright -- the unpaired orbitals are held as
    they came out of the orbital file, whatever else is being optimized.
    """
    return [parameter(f'u_{orbital},{column}', 1.0, False) for column, orbital in enumerate(unpaired, start=1)]


def geminal_section(
    occupied: list[int],
    unpaired: list[int],
    anchors: list[int],
    shells: list[list[int]],
    diag_shells: list[list[int]] | None = None,
    settings: dict | None = None,
) -> str:
    """The GEMINAL block: Hartree-Fock, the correlating geminal, its mirror, the constraints."""
    settings = settings if settings is not None else settings_for()
    diag_shells = diag_shells or []
    reference = [shell[-1] for shell in shells]
    geminals = (2, 3) if settings['mirror'] else (2,)
    seeds = [settings['seed'], settings['seed2']]

    lines = [
        'GEMINAL:',
        '  Default g optimizability: fixed',
        '  Default c optimizability: fixed',
        '  Geminal 1:',
        '    Parameters:',
        parameter('c', 1.0, False),
        *[parameter(f'g_{orbital},{orbital}', 1.0, False) for orbital in occupied],
        *unpaired_lines(unpaired),
    ]
    if not shells and not diag_shells:
        return '\n'.join([*lines, ''])

    lines += [
        '  Geminal 2:',
        '    Parameters:',
        parameter('c', 1.0, False),
        *[parameter(f'g_{orbital},{orbital}', 1.0, False) for orbital in anchors],
        *unpaired_lines(unpaired),
    ]
    # The seeds go by position over every correlated level, whether it is tied component by
    # component or on its diagonal alone: what they are for is a leading channel that starts
    # somewhere, and which levels came out rotationally closed does not bear on which are
    # leading. Everything after the second starts at zero and is moved by the optimisation.
    for i, orbital in enumerate(reference + [shell[-1] for shell in diag_shells]):
        lines.append(parameter(f'g_{orbital},{orbital}', seeds[i] if i < len(seeds) else 0.0, True))
    for i in range(len(shells)):
        for j in range(i + 1, len(shells)):
            if len(shells[i]) == len(shells[j]):
                lines.append(parameter(f'g_{reference[i]},{reference[j]}', 0.0, True))
    if settings['mirror']:
        lines += [
            '  Geminal 3:',
            '    Parameters:',
            parameter('c', -1.0, False),
            *unpaired_lines(unpaired),
        ]

    lines.append('  Constraints:')
    for shell in shells + diag_shells:
        members = [f'{n}^g_{orbital},{orbital}' for n in geminals for orbital in [shell[-1], *shell[-2::-1]]]
        lines.append('    ' + '='.join(members))
    for i in range(len(shells)):
        for j in range(i + 1, len(shells)):
            if len(shells[i]) != len(shells[j]):
                continue
            a, b = shells[i], shells[j]
            pairs = [(a[-1], b[-1]), *zip(a[-2::-1], b[-2::-1], strict=True)]
            members = [f'{n}^g_{row},{column}' for n in geminals for m, k in pairs for row, column in ((m, k), (k, m))]
            lines.append('    ' + '='.join(members))
    return '\n'.join([*lines, ''])


def occupation(neu: int, ned: int, shells: list[list[int]], diag_shells: list[list[int]], settings: dict) -> tuple[list[int], list[int], list[int]]:
    """Which MOs are doubly occupied, which are the unpaired columns, and which are anchors.

    The reference determinant, in other words, and it comes from `input`: the orbital file
    knows how many electrons the SCF had and never how many this calculation asks for. The
    unpaired columns are the MOs between ned and neu, which is the reference determinant's
    own singly occupied set.
    """
    occupied = list(range(1, ned + 1))
    unpaired = list(range(ned + 1, neu + 1))
    if settings['anchors'] is not None:
        return occupied, unpaired, [int(orbital) for orbital in settings['anchors']]
    correlated = {orbital for shell in shells + diag_shells for orbital in shell}
    return occupied, unpaired, [orbital for orbital in occupied if orbital not in correlated]


def casl(section: str, provenance: str = '') -> str:
    """The whole file. CASL strips `#` comments, so the provenance line costs nothing."""
    return (f'# {provenance}\n' if provenance else '') + section


def check(keywords: dict, channels, orbitals: dict | None, occupied, unpaired, anchors, settings: dict) -> list[str]:
    """Everything CASINO would errstop on, found before the file is written.

    Each of these ends a run that a queue has already started, and every one of them is in
    `read_geminal` or the routines under it.
    """
    from casino_mcp import input_file

    errors = []
    neu, ned = input_file.number(keywords.get('neu')), input_file.number(keywords.get('ned'))
    if neu is None or ned is None:
        errors.append('neu and ned are what say which orbitals the reference determinant fills, and this input does not set both')
    elif neu < ned:
        errors.append(
            f'neu {neu:.0f} is below ned {ned:.0f}: a geminal wave function needs at least as many up- as down-spin electrons '
            f'(READ_GEMINAL errstops). Swap the two spin channels in the input.'
        )
    if keywords.get('psi_s', '').strip().lower() != 'geminal':
        errors.append('the input this would write does not set psi_s : geminal, so CASINO would not read the GEMINAL block at all')
    if input_file.truthy(keywords.get('use_gjastrow')):
        errors.append(
            'use_gjastrow is T: the Jastrow factor then lives in a JASTROW block of this same parameters.casl, and this writes '
            'the GEMINAL block alone. Write the geminal into a directory whose Jastrow is in correlation.data (use_jastrow : T).'
        )
    if unpaired:
        if input_file.truthy(keywords.get('backflow')):
            errors.append(f'{len(unpaired)} unpaired electron(s) and backflow : T: READ_GEMINAL errstops, backflow is not implemented for them')
        if input_file.truthy(keywords.get('complex_wf')):
            errors.append(
                f'{len(unpaired)} unpaired electron(s) and complex_wf : T: READ_GEMINAL errstops, '
                f'they are not implemented for a complex wave function'
            )
    if not occupied and not unpaired:
        errors.append('the reference determinant is empty: neu and ned are both zero, and a geminal has nothing to pair')

    if orbitals is not None:
        if orbitals['periodicity']:
            errors.append(
                f'{orbitals["path"]} is a {orbitals["periodicity"]}D-periodic system: its orbital coefficients are per k-point and complex, '
                f'and the level structure this reads off them is not there to be read. A channel-less geminal needs no orbital file.'
            )
        out_of_range = sorted(orbital for orbital in anchors if orbital < 1 or orbital > orbitals['norb'])
        if out_of_range:
            errors.append(
                f'anchor orbital(s) {", ".join(str(orbital) for orbital in out_of_range)} are outside the 1..{orbitals["norb"]} the file holds'
            )
    elif channels:
        errors.append('channels were asked for and no orbital file was read: the levels they name can only come from one')
    if settings['purity'] <= 0 or settings['purity'] > 1:
        errors.append(f'purity {settings["purity"]} is not a fraction of an orbital weight in (0, 1]')
    return errors


def describe(keywords: dict, orbitals: dict | None, occupied, unpaired, anchors, shells, diag_shells, settings: dict) -> list[str]:
    """What was written that the file does not say in so many words."""
    from casino_mcp import input_file

    notes = []
    if not shells and not diag_shells:
        notes.append(
            f'no channels were asked for, so the block holds Geminal 1 alone: g_m,m = 1 over orbital(s) '
            f'{", ".join(str(orbital) for orbital in occupied) or "none"}, which is the Hartree-Fock determinant exactly and the check '
            f'the manual recommends before correlating anything'
        )
    else:
        notes.append(
            f'Geminal 2 keeps {"orbital(s) " + ", ".join(str(orbital) for orbital in anchors) if anchors else "no orbital"} fixed at 1 and '
            f'correlates {len(shells) + len(diag_shells)} level(s); the first two diagonals start at {settings["seed"]} and {settings["seed2"]} '
            f'rather than at zero, because a correlating geminal that holds only its anchors has a singular pairing matrix and no gradient to '
            f'move it off the Hartree-Fock wave function'
        )
        if settings['anchors'] is None and anchors:
            notes.append(
                'the anchors were derived: every occupied orbital no correlated level contains. Pass anchors to overrule it -- a 2s pair that '
                'the p block is meant to *replace* rather than sit beside is not an anchor, which is the Be near-degeneracy'
            )
    if unpaired:
        notes.append(
            f'{len(unpaired)} unpaired column(s), orbital(s) {", ".join(str(orbital) for orbital in unpaired)}: written into every geminal, since a '
            f'geminal with a non-zero c and an empty unpaired column has a singular matrix at every configuration, and fixed, since CASINO '
            f'refuses to optimize them'
        )
        overlap = sorted({orbital for shell in shells + diag_shells for orbital in shell} & set(unpaired))
        if overlap:
            notes.append(
                f'orbital(s) {", ".join(str(orbital) for orbital in overlap)} are both an unpaired column and a member of a correlated level: '
                f'the level is only half empty, and correlating it describes an excitation out of an orbital the reference determinant occupies'
            )
    if settings['mirror']:
        notes.append(
            'Geminal 3 is the mirror: c = -1, none of the anchors, and every one of its g tied to Geminal 2 by the constraints, so the two '
            'always hold the same numbers'
        )
    if orbitals is not None:
        electrons = (input_file.number(keywords.get('neu')) or 0) + (input_file.number(keywords.get('ned')) or 0)
        if orbitals['electrons'] and electrons and orbitals['electrons'] != electrons:
            notes.append(
                f'{orbitals["path"]} was written for {orbitals["electrons"]} electrons and this input asks for {electrons:.0f}: the orbitals are '
                f'those of a different charge state, which is legal and rarely meant'
            )
        if orbitals['unrestricted']:
            notes.append(
                'the orbital file is spin-unrestricted and the levels were read off its up-spin orbitals; CASINO pairs an up-spin orbital with a '
                'down-spin one, and the two sets need not come out of the SCF in the same order'
            )
    notes.append('the geminal parameters are values, not zeros: unlike a Jastrow factor, a pairing matrix with an empty diagonal is singular')
    return notes


def spin_check(orbitals: dict, purity: float, channels: list[tuple[int, int]]) -> list[str]:
    """Whether the down-spin orbitals of an unrestricted file fall into the same levels.

    They need not, and where they do not the constraint groups written off the up-spin set
    tie orbitals whose down-spin counterparts are something else. Cheap to check and not
    otherwise visible until the energy comes out wrong.
    """
    if not orbitals['unrestricted'] or len(orbitals['orbitals']) < 2 or not channels:
        return []
    up, down = mo_levels(orbitals, 0, purity), mo_levels(orbitals, 1, purity)
    differing = [
        channel_name(l)
        for l, count in channels
        if [members for members, _ in up.get(l, [])][:count] != [members for members, _ in down.get(l, [])][:count]
    ]
    if not differing:
        return []
    return [
        f'the up- and down-spin orbitals of this unrestricted file do not fall into the same {", ".join(sorted(set(differing)))} levels; '
        f'the constraints were written off the up-spin set'
    ]
