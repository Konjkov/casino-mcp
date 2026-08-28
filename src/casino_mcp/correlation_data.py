"""Writing a blank Jastrow factor and a blank backflow function into a `correlation.data`.

Text out, no MCP, and the same shape as `input_file`: the caller says which terms it wants,
this returns the file that asks CASINO for them. What it never contains is a parameter value --
that is what the optimisation is for, and a wave function with no coefficients is exactly how
one starts. CASINO reads such a file happily: `read_u_term` in `pjastrow.f90` fills `alpha` with
zeros before it reads anything, and on the first line it cannot parse as a number it says "Not
all coefficients supplied: rest assumed to be zero" and moves on to the next term.
`init_pbackflow` does the same and calls it "Unspecified params : Zeroed".

There is no CASINO utility that writes this file. The manual's own instruction is to copy an
example and delete the parameter lines by hand, which is fine for a person with one calculation
and no help at all for a directory that has an orbital file and nothing else. So this module
writes it, and everything it writes is a value CASINO's reader was seen to accept:

    JASTROW                                 BACKFLOW
    u    one set, l=m=0, `N_u` terms        eta  one set, `N_eta` terms, a cutoff per spin pair
    chi  one set per element, `N_chi`       mu   one set per element, `N_mu` terms
    f    one set per element, eN * ee       phi  one set per element, phi and theta over eN * ee

The two halves are the same shape twice over, and deliberately so: a `mu` set is a `chi` set
with an e-N cusp *type* instead of a flag, and `phi` is `f` with two more flags. What is not
shared is who the cusp flag belongs to. In chi it is a choice, and off unless asked for; in mu
and phi it is a *fact about the atom* -- 1 where the nucleus is bare, 0 where a pseudopotential
stands in its place -- and CASINO does not check it against the pseudopotentials it loaded. So
it is derived here from those files and not defaulted, because a wrong value is not an errstop,
it is a wrong wave function.

Cutoffs are written as zero on purpose. Every reader in both files takes a zero cutoff as "use
the default" and calls `default_L_u()`, `default_cutoff_eta(s)` and their siblings, whose
answers depend on the geometry -- 2 a.u. for a single atom against 5 for a molecule, a fraction
of the Wigner-Seitz radius when periodic. Writing a number here instead would be reimplementing
CASINO's own choice and getting it wrong for the next system. `describe` says what the zero
will become.

The geometry comes from the orbital file, because that is the only place it exists: `input`
says how many electrons there are, never how many nuclei or which. Which atoms are
pseudo-atoms comes from the `*_pp.data` files, each of which states its own atomic number --
the file names would need a table of element symbols, and the headers do not.
"""

import re
from pathlib import Path

from casino_mcp import input_file

TERMS = ('u', 'chi', 'f')
BACKFLOW_TERMS = ('eta', 'mu', 'phi')

DERIVE = -1  # a setting whose value is a fact about the system, not a preference: see `cusp_bf`

# How many parameter channels a spin dependence asks for. Pairs are for the terms between two
# electrons (u, f, eta, phi), singles for the terms between an electron and a nucleus (chi, mu).
# This is `no_spairs` / `no_ssingles`, and it decides how many cutoff lines eta gets.
SPIN_PAIRS = {0: 1, 1: 2, 2: 3}
SPIN_SINGLES = {0: 1, 1: 2}

# Every one of these is a value in the file, and every one of them is a decision the caller can
# take back. The defaults are the ones CASINO's own examples use, with three exceptions, all
# noted where they are written: the cutoffs, which are left to CASINO; the chi cusp, which is
# off because every basis this can write for either satisfies the cusp condition already or
# forbids imposing it here; and the backflow cusp type, which is derived from the atoms.
DEFAULTS = {
    'trunc_order': 3,  # C in (1 - r/L)^C: 3 is the usual choice, and 2 is the floor for an optimizable cutoff
    'n_u': 8,
    'spin_dep_u': 1,  # 0 -> uu=dd=ud; 1 -> uu=dd/=ud; 2 -> uu/=dd/=ud
    'cutoff_u': 0.0,
    'n_chi': 8,
    'spin_dep_chi': 0,  # 0 -> u=d; 1 -> u/=d
    'cutoff_chi': 0.0,
    'cusp_chi': 0,
    'n_f_en': 3,
    'n_f_ee': 3,
    'spin_dep_f': 1,
    'cutoff_f': 0.0,
    'no_dup_u': 0,  # let f duplicate what u and chi can already describe; CASINO constrains it
    'no_dup_chi': 0,
    'optimizable': 1,  # the cutoffs. The coefficients are optimizable by default and unwritten
    # Backflow. Its truncation order is a keyword of its own -- `C_trunc` in `pbackflow.f90` --
    # and CASINO's examples give it the same 3 the Jastrow gets.
    'bf_trunc_order': 3,
    'n_eta': 9,
    'spin_dep_eta': 1,
    'cutoff_eta': 0.0,
    'n_mu': 9,
    'spin_dep_mu': 0,
    'cutoff_mu': 0.0,
    'n_phi_en': 3,
    'n_phi_ee': 3,
    'spin_dep_phi': 1,
    'cutoff_phi': 0.0,
    'irrotational': 0,  # the divergence-free constraint on phi: a smaller space, and rarely wanted first
    # 1 where the nucleus is bare, 0 where a pseudopotential stands in for it. DERIVE reads that
    # off the `*_pp.data` files, which is right for every system this can write for; set it to 0
    # by hand for an all-electron atom whose orbitals do *not* satisfy the cusp condition, which
    # is what CASINO means by "cuspless AE".
    'cusp_bf': DERIVE,
}

# The header lines of the orbital file that carry the geometry. `gwfn.data` writes them with a
# colon and `stowfn.data` without one, and the values follow on the next lines -- wrapped across
# several of them when there are more atoms than fit on one, which is why the values are read by
# count rather than by line.
GEOMETRY = {
    'periodicity': re.compile(r'\s*periodicity\s*:?\s*$', re.IGNORECASE),
    'atoms': re.compile(r'\s*number of atoms\s*:?\s*$', re.IGNORECASE),
    'atomic_numbers': re.compile(r'\s*atomic numbers for each atom\s*:?\s*$', re.IGNORECASE),
    'valence_charges': re.compile(r'\s*valence charges for each atom\s*:?\s*$', re.IGNORECASE),
}

PP_ATOMIC_NUMBER = re.compile(r'\s*atomic number and pseudo-charge\s*$', re.IGNORECASE)

# `gwfn.data` and `stowfn.data` give the geometry a labelled line per quantity, which is what
# `read_geometry` reads. `pwfn.data` and `bwfn.data` write "Number of atoms per primitive cell"
# and then the atomic number and the position of one atom on each line -- a different reader,
# for a file a periodic code wrote, and a periodic Jastrow is not what this writes anyway.
UNREADABLE_GEOMETRY = {'plane-wave': 'pwfn.data', 'blip': 'bwfn.data'}

# Bases whose orbitals do not satisfy the electron-nucleus cusp condition by themselves, and
# whose all-electron atoms CASINO therefore suggests a chi cusp for (`read_chi_term`). A
# gaussian basis is not among them: `cusp_correction` is on by default and fixes the orbitals,
# which is why every all-electron gaussian example in this repository imposes no cusp.
CUSPLESS_BASES = ('plane-wave', 'blip')


def numbers_after(lines: list[str], start: int, count: int, what: str) -> list[float]:
    """The next `count` numbers after line `start`, however many lines they are spread over."""
    values = []
    for line in lines[start + 1 :]:
        tokens = [input_file.number(token) for token in line.split()]
        if None in tokens or not tokens:
            break
        values.extend(tokens)
        if len(values) >= count:
            return values[:count]
    raise ValueError(f'{what}: found {len(values)} of the {count} numbers the file says are there')


def read_geometry(path) -> dict:
    """The atoms of a calculation, out of the header of its orbital file.

    Only the header: the basis set and the orbital coefficients are the bulk of the file and
    none of this module's business. A `bwfn.data.bin` has no header to read, and says so.
    """
    path = Path(path)
    text = path.read_text(errors='replace')
    lines = text.splitlines()
    marks = {}
    for i, line in enumerate(lines):
        for name, pattern in GEOMETRY.items():
            if name not in marks and pattern.match(line):
                marks[name] = i
    for name in ('atoms', 'atomic_numbers'):
        if name not in marks:
            raise ValueError(f'{path} has no "{name.replace("_", " ")}" line: this is not an orbital file this can read the geometry from')
    count = int(numbers_after(lines, marks['atoms'], 1, f'{path}: number of atoms')[0])
    atomic_numbers = [int(z) for z in numbers_after(lines, marks['atomic_numbers'], count, f'{path}: atomic numbers')]
    valence = numbers_after(lines, marks['valence_charges'], count, f'{path}: valence charges') if 'valence_charges' in marks else []
    periodicity = int(numbers_after(lines, marks['periodicity'], 1, f'{path}: periodicity')[0]) if 'periodicity' in marks else 0
    return {
        'path': str(path),
        'periodicity': periodicity,
        'atomic_numbers': atomic_numbers,
        'valence_charges': valence,
        'sets': species_sets(atomic_numbers),
    }


def species_sets(atomic_numbers: list[int]) -> list[dict]:
    """One set per element, holding the labels of its atoms, in the order the file lists them.

    Set by element and not by atom: a Jastrow with a set per atom has as many cutoffs and
    coefficients as there are nuclei and nothing to gain by it, since symmetry-equivalent atoms
    optimize to the same numbers. Labels are the atom's position in the orbital file, 1-based,
    which is what CASINO means by labelling style 1 -- the atom in the simulation cell.
    """
    groups: dict[int, list[int]] = {}
    for label, z in enumerate(atomic_numbers, start=1):
        groups.setdefault(z, []).append(label)
    return [{'z': z, 'labels': labels} for z, labels in groups.items()]


def pseudo_species(directory) -> set[int]:
    """The atomic numbers that have a pseudopotential in this directory.

    Read out of the files rather than off their names: `b_pp.data` needs a table of element
    symbols to interpret, and the second line of every one of them says "Atomic number and
    pseudo-charge" over the two numbers themselves.
    """
    found = set()
    for path in sorted(Path(directory).glob('*_pp.data')):
        lines = path.read_text(errors='replace').splitlines()
        for i, line in enumerate(lines[:-1]):
            if PP_ATOMIC_NUMBER.match(line):
                atomic_number = input_file.number(lines[i + 1].split()[0]) if lines[i + 1].split() else None
                if atomic_number is not None:
                    found.add(int(atomic_number))
                break
    return found


def settings_for(overrides: dict | None = None) -> dict:
    """The defaults with the caller's changes folded in, and nothing invented."""
    overrides = {name.lower(): value for name, value in (overrides or {}).items()}
    unknown = sorted(name for name in overrides if name not in DEFAULTS)
    if unknown:
        raise KeyError(f'no such Jastrow setting: {", ".join(unknown)}. Known: {", ".join(sorted(DEFAULTS))}')
    return {**DEFAULTS, **overrides}


def marked(text: str) -> str:
    """A label line, in the column CASINO's own `write_jastrow` puts it in."""
    return f' {text}'


def valued(value) -> str:
    return f'   {value}'


def cutoff_line(cutoff, optimizable) -> str:
    """`cutoff ; optimizable`, where a cutoff of zero asks CASINO for its own default."""
    return f'   {repr(float(cutoff)):<34}{optimizable}'


def label_line(labels: list[int]) -> str:
    return ''.join(f'{label:>5}' for label in labels)


def u_term(settings: dict) -> list[str]:
    return [
        marked('START U TERM'),
        marked('Number of sets'),
        valued(1),
        marked('START SET 1'),
        marked('Spherical harmonic l,m'),
        valued('0 0'),
        marked('Expansion order N_u'),
        valued(settings['n_u']),
        marked('Spin dep (0->uu=dd=ud; 1->uu=dd/=ud; 2->uu/=dd/=ud)'),
        valued(settings['spin_dep_u']),
        marked('Cutoff (a.u.)     ;  Optimizable (0=NO; 1=YES)'),
        cutoff_line(settings['cutoff_u'], settings['optimizable']),
        marked('Parameter values  ;  Optimizable (0=NO; 1=YES)'),
        marked('END SET 1'),
        marked('END U TERM'),
    ]


def chi_term(sets: list[dict], settings: dict) -> list[str]:
    lines = [
        marked('START CHI TERM'),
        marked('Number of sets ; labelling (1->atom in s. cell; 2->atom in p. cell; 3->species)'),
        valued(f'{len(sets)} 1'),
    ]
    for index, group in enumerate(sets, start=1):
        labels = group['labels']
        lines += [
            marked(f'START SET {index}'),
            marked('Spherical harmonic l,m'),
            valued('0 0'),
            marked('Number of atoms in set'),
            valued(len(labels)),
            marked('Label of the atom in this set' if len(labels) == 1 else 'Labels of the atoms in this set'),
            label_line(labels),
            marked('Impose electron-nucleus cusp (0=NO; 1=YES)'),
            valued(settings['cusp_chi']),
            marked('Expansion order N_chi'),
            valued(settings['n_chi']),
            marked('Spin dep (0->u=d; 1->u/=d)'),
            valued(settings['spin_dep_chi']),
            marked('Cutoff (a.u.)     ;  Optimizable (0=NO; 1=YES)'),
            cutoff_line(settings['cutoff_chi'], settings['optimizable']),
            marked('Parameter values  ;  Optimizable (0=NO; 1=YES)'),
            marked(f'END SET {index}'),
        ]
    return lines + [marked('END CHI TERM')]


def f_term(sets: list[dict], settings: dict) -> list[str]:
    lines = [
        marked('START F TERM'),
        marked('Number of sets ; labelling (1->atom in s. cell; 2->atom in p. cell; 3->species)'),
        valued(f'{len(sets)} 1'),
    ]
    for index, group in enumerate(sets, start=1):
        labels = group['labels']
        lines += [
            marked(f'START SET {index}'),
            marked('Number of atoms in set'),
            valued(len(labels)),
            marked('Label of the atom in this set' if len(labels) == 1 else 'Labels of the atoms in this set'),
            label_line(labels),
            marked('Prevent duplication of u term (0=NO; 1=YES)'),
            valued(settings['no_dup_u']),
            marked('Prevent duplication of chi term (0=NO; 1=YES)'),
            valued(settings['no_dup_chi']),
            marked('Electron-nucleus expansion order N_f_eN'),
            valued(settings['n_f_en']),
            marked('Electron-electron expansion order N_f_ee'),
            valued(settings['n_f_ee']),
            marked('Spin dep (0->uu=dd=ud; 1->uu=dd/=ud; 2->uu/=dd/=ud)'),
            valued(settings['spin_dep_f']),
            marked('Cutoff (a.u.)     ;  Optimizable (0=NO; 1=YES)'),
            cutoff_line(settings['cutoff_f'], settings['optimizable']),
            marked('Parameter values  ;  Optimizable (0=NO; 1=YES)'),
            marked(f'END SET {index}'),
        ]
    return lines + [marked('END F TERM')]


def jastrow(geometry: dict, terms=TERMS, title: str = '', settings: dict | None = None) -> str:
    """The JASTROW block for one system, with no parameter values in it."""
    settings = settings if settings is not None else settings_for()
    sets = geometry['sets']
    lines = [
        marked('START JASTROW'),
        marked('Title'),
        marked(title or 'No title given.'),
        marked('Truncation order C'),
        valued(settings['trunc_order']),
    ]
    if 'u' in terms:
        lines += u_term(settings)
    if 'chi' in terms:
        lines += chi_term(sets, settings)
    if 'f' in terms:
        lines += f_term(sets, settings)
    return '\n'.join([*lines, marked('END JASTROW'), ''])


def is_all_electron(group: dict, settings: dict, pseudo: set[int]) -> int:
    """The e-N cusp type of one backflow set: 1 for a bare nucleus, 0 behind a pseudopotential.

    Not a preference. CASINO reads this flag and believes it -- there is no check anywhere in
    `init_pbackflow` against the pseudopotentials it has already loaded -- so getting it wrong
    is not an errstop but a backflow function that imposes the wrong condition at the nucleus.
    """
    if settings['cusp_bf'] != DERIVE:
        return int(settings['cusp_bf'])
    return int(group['z'] not in pseudo)


def eta_term(settings: dict) -> list[str]:
    """The e-e backflow term. One cutoff line per spin-pair channel, and CASINO wants them all.

    Unlike every other cutoff in either block, eta's is per channel: `read_cutoff_eta` loops over
    `no_spin_params_eta` lines and errstops outright if the first is missing. (An optimizable
    flag of 2 would mean "one cutoff for all channels"; writing them out is plainer.) A zero is
    still a zero: `default_cutoff_eta(s)` answers 1 a.u. for the channels that carry the e-e
    cusp and 4 a.u. for the rest.
    """
    channels = SPIN_PAIRS.get(settings['spin_dep_eta'], 1)
    return [
        marked('START ETA TERM'),
        marked('Expansion order'),
        valued(settings['n_eta']),
        marked('Spin dep (0->uu=dd=ud; 1->uu=dd/=ud; 2->uu/=dd/=ud)'),
        valued(settings['spin_dep_eta']),
        marked('Cut-off radii ;      Optimizable (0=NO; 1=YES; 2=YES BUT NO SPIN-DEP)'),
        *[f'{cutoff_line(settings["cutoff_eta"], settings["optimizable"])}       ! L_{channel}' for channel in range(1, channels + 1)],
        marked('Parameter ;          Optimizable (0=NO; 1=YES)'),
        marked('END ETA TERM'),
    ]


def mu_term(sets: list[dict], settings: dict, pseudo: set[int]) -> list[str]:
    """The e-N backflow term: a chi set with a cusp *type* where chi has a cusp *choice*."""
    lines = [
        marked('START MU TERM'),
        marked('Number of sets ; labelling (1->atom in s. cell; 2->atom in p. cell; 3->species)'),
        valued(f'{len(sets)} 1'),
    ]
    for index, group in enumerate(sets, start=1):
        labels = group['labels']
        lines += [
            marked(f'START SET {index}'),
            marked('Number of atoms in set'),
            valued(len(labels)),
            marked('Label of the atom in this set' if len(labels) == 1 else 'Labels of the atoms in this set'),
            label_line(labels),
            marked('Type of e-N cusp conditions (0->PP/cuspless AE; 1->AE with cusp)'),
            valued(is_all_electron(group, settings, pseudo)),
            marked('Expansion order'),
            valued(settings['n_mu']),
            marked('Spin dep (0->u=d; 1->u/=d)'),
            valued(settings['spin_dep_mu']),
            marked('Cutoff (a.u.)     ;  Optimizable (0=NO; 1=YES)'),
            cutoff_line(settings['cutoff_mu'], settings['optimizable']),
            marked('Parameter values  ;  Optimizable (0=NO; 1=YES)'),
            marked(f'END SET {index}'),
        ]
    return lines + [marked('END MU TERM')]


def phi_term(sets: list[dict], settings: dict, pseudo: set[int]) -> list[str]:
    """The e-e-N backflow term: `f` with a cusp type and the irrotational flag added.

    One block writes both phi and theta -- `write_pbackflow` prints them one after the other
    under a single "Parameter values" line -- so a blank one is a blank one for both.
    """
    lines = [
        marked('START PHI TERM'),
        marked('Number of sets ; labelling (1->atom in s. cell; 2->atom in p. cell; 3->species)'),
        valued(f'{len(sets)} 1'),
    ]
    for index, group in enumerate(sets, start=1):
        labels = group['labels']
        lines += [
            marked(f'START SET {index}'),
            marked('Number of atoms in set'),
            valued(len(labels)),
            marked('Label of the atom in this set' if len(labels) == 1 else 'Labels of the atoms in this set'),
            label_line(labels),
            marked('Type of e-N cusp conditions (0=PP; 1=AE)'),
            valued(is_all_electron(group, settings, pseudo)),
            marked('Irrotational Phi term (0=NO; 1=YES)'),
            valued(settings['irrotational']),
            marked('Electron-nucleus expansion order N_eN'),
            valued(settings['n_phi_en']),
            marked('Electron-electron expansion order N_ee'),
            valued(settings['n_phi_ee']),
            marked('Spin dep (0->uu=dd=ud; 1->uu=dd/=ud; 2->uu/=dd/=ud)'),
            valued(settings['spin_dep_phi']),
            marked('Cutoff (a.u.)     ;  Optimizable (0=NO; 1=YES)'),
            cutoff_line(settings['cutoff_phi'], settings['optimizable']),
            marked('Parameter values  ;  Optimizable (0=NO; 1=YES)'),
            marked(f'END SET {index}'),
        ]
    return lines + [marked('END PHI TERM')]


def backflow_block(geometry: dict, terms=BACKFLOW_TERMS, title: str = '', settings: dict | None = None, pseudo: set[int] | None = None) -> str:
    """The BACKFLOW block for one system, with no parameter values in it.

    No AE CUTOFFS section: it is optional, and CASINO builds one itself when the atoms need it
    -- `if(.not.ae_block_present)` gives every all-electron nucleus a set of its own with the
    length it would have defaulted to. Writing one here would be stating a number CASINO already
    knows how to choose.
    """
    settings = settings if settings is not None else settings_for()
    sets, pseudo = geometry['sets'], pseudo or set()
    lines = [
        marked('START BACKFLOW'),
        marked('Title'),
        marked(title or 'No title given.'),
        marked('Truncation order'),
        valued(settings['bf_trunc_order']),
    ]
    if 'eta' in terms:
        lines += eta_term(settings)
    if 'mu' in terms:
        lines += mu_term(sets, settings, pseudo)
    if 'phi' in terms:
        lines += phi_term(sets, settings, pseudo)
    return '\n'.join([*lines, marked('END BACKFLOW'), ''])


def blank(
    geometry: dict,
    terms=TERMS,
    backflow=(),
    title: str = '',
    settings: dict | None = None,
    pseudo: set[int] | None = None,
) -> str:
    """A whole `correlation.data`: a blank Jastrow factor, a blank backflow function, or both.

    The HEADER and VERSION blocks are optional -- `read_correlation_header` says so out loud
    when they are absent -- and are written anyway, in the layout CASINO's own
    `write_correlation_header` uses, so that a file this wrote and a file CASINO rewrote after
    an optimisation are the same kind of document.
    """
    header = [
        marked('START HEADER'),
        marked(title or 'No title given.'),
        marked('END HEADER'),
        '',
        marked('START VERSION'),
        valued(1),
        marked('END VERSION'),
        '',
    ]
    text = '\n'.join(header) + '\n'
    if terms:
        text += jastrow(geometry, terms=terms, title=title, settings=settings)
    if backflow:
        text += ('\n' if terms else '') + backflow_block(geometry, terms=backflow, title=title, settings=settings, pseudo=pseudo)
    return text


def check(
    geometry: dict,
    terms=TERMS,
    settings: dict | None = None,
    pseudo: set[int] | None = None,
    basis: str = '',
    backflow=(),
) -> list[str]:
    """Everything CASINO would errstop on, found before the file is written.

    Each of these is a line in `read_u_term`, `read_chi_term`, `read_f_term` or `init_pbackflow`
    that ends the run -- and the run ends after the queue has already started it, which is the
    whole reason for checking here.
    """
    settings = settings if settings is not None else settings_for()
    pseudo = pseudo or set()
    errors = []
    unknown = [name for name in terms if name not in TERMS]
    if unknown:
        errors.append(f'no such Jastrow term: {", ".join(unknown)}. This writes {", ".join(TERMS)}')
    if not terms and not backflow:
        errors.append('no terms asked for: a correlation.data with neither a Jastrow factor nor a backflow function is an empty file')
    if geometry['periodicity']:
        errors.append(
            f'{geometry["path"]} is a {geometry["periodicity"]}D-periodic system. '
            f"A periodic Jastrow wants a P term, whose stars of reciprocal lattice vectors come from CASINO's own "
            f'make_p_stars; only finite systems for now'
        )
    if not geometry['atomic_numbers'] and ({'chi', 'f'} & set(terms) or {'mu', 'phi'} & set(backflow)):
        errors.append('chi, f, mu and phi are electron-nucleus terms, and this system has no atoms')
    if settings['trunc_order'] < 2 and settings['optimizable'] and terms:
        errors.append(f'truncation order {settings["trunc_order"]} cannot have an optimizable cutoff: CASINO wants C >= 2 for that')
    if settings['trunc_order'] < 0:
        errors.append(f'truncation order {settings["trunc_order"]} is negative')
    if 'u' in terms and settings['n_u'] < 1:
        errors.append(f'N_u {settings["n_u"]} is below 1')
    if 'chi' in terms and settings['n_chi'] < 1:
        errors.append(f'N_chi {settings["n_chi"]} is below 1')
    if 'f' in terms and (settings['n_f_en'] < 0 or settings['n_f_ee'] < 0):
        errors.append(f'N_f_eN {settings["n_f_en"]} and N_f_ee {settings["n_f_ee"]} must both be at least 0')
    errors.extend(check_cusp(geometry, terms, settings, pseudo, basis))
    errors.extend(check_backflow(geometry, backflow, settings, pseudo))
    return errors


def check_backflow(geometry: dict, backflow, settings: dict, pseudo: set[int]) -> list[str]:
    """What `init_pbackflow` refuses, and the one thing it does not refuse but should.

    The expansion orders are the same kind of floor the Jastrow has. The free-parameter count is
    not: an all-electron mu set spends one parameter per spin channel on the cusp condition, so
    a low order that is fine for a pseudo-atom leaves a bare nucleus with nothing to optimize --
    'No free parameters in set.', after the queue has started the job.
    """
    if not backflow:
        return []
    errors = []
    unknown = [name for name in backflow if name not in BACKFLOW_TERMS]
    if unknown:
        errors.append(f'no such backflow term: {", ".join(unknown)}. This writes {", ".join(BACKFLOW_TERMS)}')
    if settings['bf_trunc_order'] < 0:
        errors.append(f'backflow truncation order {settings["bf_trunc_order"]} is negative')
    if 'eta' in backflow and settings['n_eta'] < 1:
        errors.append(f'N_eta {settings["n_eta"]} is below 1')
    if settings['spin_dep_eta'] not in SPIN_PAIRS and 'eta' in backflow:
        errors.append(f'spin_dep_eta {settings["spin_dep_eta"]} is not one of {", ".join(str(value) for value in SPIN_PAIRS)}')
    if 'phi' in backflow and (settings['n_phi_en'] < 1 or settings['n_phi_ee'] < 1):
        errors.append(f'N_eN {settings["n_phi_en"]} and N_ee {settings["n_phi_ee"]} must both be at least 1 for phi')
    if 'phi' in backflow:
        errors.extend(check_phi_parameters(geometry, settings, pseudo))
    if 'mu' in backflow:
        errors.extend(check_mu_parameters(geometry, settings, pseudo))
    return errors


def check_phi_parameters(geometry: dict, settings: dict, pseudo: set[int]) -> list[str]:
    """The floor an all-electron phi set puts under the electron-nucleus expansion order.

    Counting the free phi and theta parameters means solving the cusp, no-duplication and
    (optionally) irrotational constraints, which is `find_determined_phi_theta` and not
    something to restate here. What is worth restating is where the count reaches zero, and that
    was found by putting the orders to CASINO one by one: an all-electron set with N_eN = 1 has
    no free parameters whatever N_ee is, and a pseudo-atom set is free at N_eN = 1. N_ee never
    decides it.
    """
    if settings['n_phi_en'] >= 2:
        return []
    bare = [group for group in geometry['sets'] if is_all_electron(group, settings, pseudo)]
    if not bare:
        return []
    return [
        f'the phi set for Z={", ".join(str(group["z"]) for group in bare)} is all-electron, and an all-electron phi set with '
        f'N_eN {settings["n_phi_en"]} has no free parameters left once the cusp conditions are imposed. Raise n_phi_en to 2 or more'
    ]


def check_mu_parameters(geometry: dict, settings: dict, pseudo: set[int]) -> list[str]:
    """`(N_mu - 1) * channels`, less one channel per set that carries an all-electron cusp."""
    if settings['n_mu'] < 1:
        return [f'N_mu {settings["n_mu"]} is below 1']
    channels = SPIN_SINGLES.get(settings['spin_dep_mu'])
    if channels is None:
        return [f'spin_dep_mu {settings["spin_dep_mu"]} is not one of {", ".join(str(value) for value in SPIN_SINGLES)}']
    for group in geometry['sets']:
        free = channels * (settings['n_mu'] - is_all_electron(group, settings, pseudo))
        if free < 1:
            return [
                f'the mu set for Z={group["z"]} would have no free parameters: expansion order {settings["n_mu"]} over '
                f'{channels} spin channel(s), less the one an all-electron cusp condition fixes. Raise n_mu'
            ]
    return []


def check_cusp(geometry: dict, terms, settings: dict, pseudo: set[int], basis: str) -> list[str]:
    """Who is allowed to impose the electron-nucleus cusp in the chi term, and who is not.

    A pseudo-atom has no cusp to impose and CASINO refuses; so does a Slater-type basis, whose
    orbitals already satisfy the condition. Both are errstops in `read_chi_term`, and both are
    reachable from an innocent-looking `cusp_chi=1`.
    """
    if 'chi' not in terms or not settings['cusp_chi']:
        return []
    errors = []
    both = sorted(set(geometry['atomic_numbers']) & pseudo)
    if both:
        errors.append(
            f'cusp_chi is 1 and Z={", ".join(str(z) for z in both)} has a pseudopotential in this directory: '
            f'there is no nuclear cusp to impose on a pseudo-atom'
        )
    if basis.strip().lower() == 'slater-type':
        errors.append(
            'cusp_chi is 1 and the basis is slater-type: those orbitals already satisfy the cusp condition, and CASINO refuses to impose it twice'
        )
    return errors


def defaulted_cutoff(term: str, geometry: dict, settings: dict) -> float | None:
    """What CASINO will put in place of a cutoff of zero, for a finite system.

    `default_L_u`, `default_L_chi` and `default_L_f` in `pjastrow.f90`, `default_cutoff_mu` and
    `default_cutoff_phi` in `pbackflow.f90`, and only the branch this module can write for: a
    periodic cell's default is a fraction of its Wigner-Seitz radius, which needs the lattice
    vectors, and a truncation order below 2 is refused before this. `eta` is not here because
    its default differs per spin channel -- 1 a.u. where the channel carries the e-e cusp, 4
    elsewhere -- and which channel that is, is CASINO's `which_spair` to decide.

    This is reported, never written: the file says zero, and CASINO chooses.
    """
    if geometry['periodicity']:
        return None
    if term in TERMS and settings['trunc_order'] < 2:
        return None
    if term == 'u':
        return 2.0 if len(geometry['atomic_numbers']) == 1 else 5.0
    return {'chi': 4.0, 'f': 3.0, 'mu': 4.5, 'phi': 4.5}.get(term)


def describe(
    geometry: dict,
    terms=TERMS,
    settings: dict | None = None,
    pseudo: set[int] | None = None,
    basis: str = '',
    backflow=(),
) -> list[str]:
    """What was written that the file does not say in so many words."""
    settings = settings if settings is not None else settings_for()
    pseudo = pseudo or set()
    notes = []
    asked = set(terms) | set(backflow)
    zeroed = [f'cutoff_{term}' for term in ('u', 'chi', 'f', 'mu', 'phi') if term in asked and not settings[f'cutoff_{term}']]
    if zeroed:
        chosen = ', '.join(f'{name} {defaulted_cutoff(name.split("_")[1], geometry, settings)}' for name in zeroed)
        notes.append(
            f'the cutoffs are written as zero, which CASINO reads as "use the default": {chosen} a.u. for this system, and optimizable from there. '
            f'Pass a number to set one instead'
        )
    if 'eta' in asked and not settings['cutoff_eta']:
        notes.append(
            "eta's cutoffs are written as zero too, one line per spin-pair channel: CASINO answers 1 a.u. for the channels that carry "
            'the e-e cusp and 4 a.u. for the rest, which is why they are separate values in the first place'
        )
    all_electron = sorted(set(geometry['atomic_numbers']) - pseudo)
    if all_electron and basis.strip().lower() in CUSPLESS_BASES and not settings['cusp_chi']:
        notes.append(
            f'Z={", ".join(str(z) for z in all_electron)} is all-electron on a {basis.strip()} basis and the chi cusp is not imposed: '
            f'CASINO will suggest imposing it unless use_gpcc is on. Pass cusp_chi=1 to impose it here'
        )
    if 'f' in terms and 'u' in terms and not settings['no_dup_u']:
        notes.append(
            "the f term is free to duplicate u and chi (no_dup_u, no_dup_chi are 0), which is what CASINO's own examples do; "
            'the duplication is a redundancy in the parameters, not an error'
        )
    notes.extend(describe_backflow(geometry, backflow, settings, pseudo))
    notes.append('no parameter values were written: every coefficient starts at zero, which is what the first optimisation cycle is for')
    return notes


def describe_backflow(geometry: dict, backflow, settings: dict, pseudo: set[int]) -> list[str]:
    """The two things about a backflow function that are decided rather than defaulted."""
    if not backflow:
        return []
    notes = []
    if {'mu', 'phi'} & set(backflow):
        bare = sorted(z for z in {group['z'] for group in geometry['sets']} if z not in pseudo)
        behind = sorted(z for z in {group['z'] for group in geometry['sets']} if z in pseudo)
        if settings['cusp_bf'] == DERIVE:
            notes.append(
                f'the e-N cusp type was derived from the pseudopotentials in the directory: '
                f'{"Z=" + ", ".join(str(z) for z in bare) + " all-electron (1)" if bare else "no all-electron atoms"}, '
                f'{"Z=" + ", ".join(str(z) for z in behind) + " behind a pseudopotential (0)" if behind else "no pseudo-atoms"}. '
                f'CASINO does not check this flag against the pseudopotentials it loads, so pass cusp_bf only for a cuspless all-electron orbital set'
            )
        else:
            notes.append(
                f'the e-N cusp type was set to {settings["cusp_bf"]} by hand for every backflow set, rather than derived from the pseudopotentials'
            )
    if any(z not in pseudo for z in geometry['atomic_numbers']):
        notes.append(
            'no AE CUTOFFS block was written: it is optional, and CASINO gives every all-electron nucleus a set of its own with the '
            'length it would have defaulted to. It appears in the correlation.out an optimisation writes'
        )
    return notes
