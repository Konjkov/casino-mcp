"""casino-mcp: an MCP control plane over the Fortran CASINO code.

The layers, innermost first:

    parse_out   a CASINO `out` file, and the `dmc.status` beside it -> structured phases.
    input_file  a CASINO `input` file: read it, edit it, write one for a runtype.
    correlation_data  a blank Jastrow factor for a calculation that has none.
    settings    where CASINO is and where our state goes: the environment, and defaults.
    jobs        the on-disk job registry under $XDG_STATE_HOME/casino-mcp.
    runtime     prepare / start / status / stop / results, over `runqmc` and `haltqmc`.
    server      the MCP tool surface. A thin wrapper over `runtime`.
    cli         the same thing for a shell.

The two innermost modules have no MCP in them and no dependencies: a path in, a dict out.
"""

__version__ = '0.4.0'

__all__ = ['__version__']
