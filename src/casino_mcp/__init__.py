"""casino-mcp: an MCP control plane over the Fortran CASINO code.

The layers, innermost first:

    parse_out   a CASINO `out` file -> structured phases. No MCP, no dependencies.
    settings    where CASINO is and where our state goes: the environment, and defaults.
    jobs        the on-disk job registry under $XDG_STATE_HOME/casino-mcp.
    runtime     start / status / stop over `runqmc` and `haltqmc`, via a launcher process.
    server      the MCP tool surface. A thin wrapper over `runtime`.
    cli         the same thing for a shell.
"""

__version__ = '0.2.0'

__all__ = ['__version__']
