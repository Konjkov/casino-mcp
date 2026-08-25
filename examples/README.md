# Example calculations

Eighteen real CASINO calculations, committed with the `out` files they produced. They are what
the test suite parses and what `casino-mcp` is demonstrated on, and they live here rather than
somewhere else on the machine: installing casino-mcp does not install PyCasino, so nothing in
the tests may point at its 526-calculation tree.

These eighteen are a **cover, not a sample**. They were picked so that each distinct setting —
runtype, basis type, sampling method, optimiser, wavefunction option — appears at least once,
at the smallest total size that achieves it. `tests/test_examples.py` asserts that the cover
still holds, so an example cannot be dropped without the suite saying which setting went with
it.

| calculation | e↑/e↓ | runtype | basis | what it is here for |
|---|---|---|---|---|
| `backflow/3_1_1/25` | 2/2 | vmc_opt | slater-type | backflow optimised by `emin`, no Jastrow |
| `geminal/B/UHF/cc-pVQZ/EBES/Jastrow_emin` | 3/2 | vmc_opt | gaussian | geminal held fixed (`opt_geminal : F`) while the Jastrow is optimised |
| `geminal/Be/HF/cc-pVQZ/EBES/Jastrow_emin__2_geminal__next` | 2/2 | vmc_opt | gaussian | `opt_cycles : 4`, geminal optimised alongside the Jastrow |
| `geminal/Ne/HF/cc-pVQZ/EBES/Jastrow_emin__extended` | 5/5 | vmc_opt | gaussian | **interrupted** between optimisation cycles: four results, no ending |
| `geminal/Ne/MP2-CASSCF(8.13)/cc-pVQZ/EBES/Jastrow_dmc` | 5/5 | vmc_dmc | gaussian | DMC on a geminal wavefunction, `dmc_method : 2` |
| `gwfn/Be/MP2-CASSCF(2.4)/cc-pVQZ/CBCS/Jastrow_emin` | 2/2 | vmc_opt | gaussian | multideterminant: `opt_det_coeff : T` |
| `gwfn/Kr/HF/cc-pVQZ/CBCS/Slater` | 18/18 | vmc | gaussian | **stopped before printing any energy** — the file a parser must not invent a result for |
| `ppotential_DF/B/HF/aug-cc-pVQZ-CDF/CBCS/Slater` | 2/1 | vmc | gaussian | Dirac–Fock pseudopotential, `cusp_correction : F` |
| `ppotential_HF/H/HF/aug-cc-pVQZ-CDF/CBCS/Jastrow_dmc` | 1/0 | vmc_dmc | gaussian | open-shell (no down electrons), T-moves on |
| `ppotential_HF/O/HF/aug-cc-pVQZ-CDF/CBCS/Backflow_emin` | 4/2 | vmc_opt | gaussian | backflow with a pseudopotential, `opt_fixnl : T` |
| `solid/Si_test` | 32/32 | vmc | plane-wave | the only **periodic** system: Ewald interaction, real orbitals |
| `step_profile/casino_ne/0.05439.7` | 5/5 | vmc | gaussian | a fixed VMC time step (`opt_dtvmc : 0`) |
| `step_profile/casino_ne/auto.3` | 5/5 | vmc | gaussian | the same system with the time step optimised (`opt_dtvmc : 1`) |
| `stowfn/He/HF/QZ4P/CBCS/Geminal` | 1/1 | vmc | slater-type | `psi_s : geminal` with no Jastrow at all |
| `stowfn/He/HF/QZ4P/CBCS/Gjastrow` | 1/1 | vmc | slater-type | the CASL Jastrow (`use_gjastrow : T`) |
| `stowfn/He/HF/QZ4P/CBCS/Jastrow_varmin` | 1/1 | vmc_opt | slater-type | the other optimiser: `varmin`, unreweighted, with filtering |
| `stowfn/He/HF/QZ4P/EBES/Slater` | 1/1 | vmc | slater-type | the cheapest run in the tree — what the MCP smoke test re-runs |
| `stowfn/Ne/HF/QZ4P/EBES/Jastrow_dmc` | 5/5 | vmc_dmc | slater-type | all-electron DMC, no pseudopotential |

`EBES` and `CBCS` in the paths are the two VMC algorithms — electron-by-electron
(`vmc_method : 1`, CASINO's default) and configuration-by-configuration (`vmc_method : 3`).

## What is in a directory, and what is not

Each holds the `input` CASINO was given, the wavefunction it read (`gwfn.data`, `stowfn.data`,
`pwfn.data`), any pseudopotential, the `correlation.data` or `parameters.casl` that carried the
Jastrow, and the `out` it wrote. That is enough to re-run the calculation.

Deliberately absent: `config.in` / `config.out`, `vmc.hist` / `dmc.hist`, and the quantum
chemistry logs the wavefunctions came from. They are large, no test reads them, and a `.hist`
file is the input to reblocking — a later stage than this parser.

## Do not run in place

Every `out` here is committed reference data, and the whole suite is validated against it.
`casino_run` refuses a directory whose `out` git tracks, which is exactly this one:

```
examples/solid/Si_test/out is committed reference data.
Copy the directory and run there, or pass overwrite=true to destroy it.
```

Copy the directory somewhere else and run there. The same applies by hand — `runqmc` in one of
these directories overwrites the file the tests compare against.

## Provenance

All eighteen come from the examples tree of [PyCasino](https://github.com/Konjkov/pycasino),
where they were produced by the Fortran CASINO releases named in each `out` header. Paths are
kept exactly as they are there, so a failure seen here is looked up there unchanged.
