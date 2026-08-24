# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions follow
[semantic versioning](https://semver.org/) — pre-1.0, so the minor version may break things.

## [Unreleased]

## [0.1.0] — 2026-08-23

First packaged release. The control plane works and is tested; the tools that return physics
to the model are not shipped yet.

### Added

- MCP server over stdio with four tools: `casino_run`, `casino_status`, `casino_stop`,
  `casino_list_jobs`. No generic shell tool, by design.
- An on-disk job registry under `$XDG_STATE_HOME/casino-mcp`, so a calculation outlives the
  server that started it, and a launcher process per job, so the whole tree
  (runqmc → mpirun → casino) can be signalled as one group and its exit code recorded.
- `parse_out`: a CASINO `out` file as structured phases, every number carrying the line it
  was read from. No MCP, no dependencies. Checked against `envmc` over 526 example files.
- Configuration through the environment, which is what `.mcp.json` can set per server
  registration: `CASINO_HOME`, `CASINO_ARCH`, `CASINO_RUNQMC`, `CASINO_MCP_STATE_DIR`,
  `CASINO_MCP_FORBID`. Every other default is a constant in `settings.py`.
- A `casino-mcp` command: `serve`, `config`, `run`, `status`, `stop`, `jobs`, `parse`.
- Guardrails against destroying results: a directory that already holds an `out`, one whose
  `out` is tracked by git, one another runqmc has locked, and a `$CASINO_MCP_FORBID` list
  that has no per-call override.
- Provenance: every job record freezes the command and the path, size and mtime of the
  `casino` binary it ran.
- Tests: 102 unit tests that need no CASINO (a fake `runqmc` stands in for the real one), plus
  an opt-in integration suite that drives real calculations over real stdio MCP.

### Notes

- `parse_out` derives exactly one number: the sample-variance error of a single-block run,
  which CASINO does not print. It is taken from the one block as `envmc` does, and labelled.
- Nothing shells out to `envmc`/`endmc` at runtime; `endmc` misparses numbers under a non-C
  locale.

[Unreleased]: https://github.com/Konjkov/casino-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Konjkov/casino-mcp/releases/tag/v0.1.0
