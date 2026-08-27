# Minimal container for casino-mcp. Used by Glama's introspection checks and anyone who
# wants to run the server in a sandbox instead of installing it on the host.
#
# The image deliberately does not contain CASINO. The server starts and answers
# `tools/list` with no installation present -- a tool call is what refuses, not the
# process -- which is all the build test needs. To actually run a calculation, mount the
# installation and the working directory, and point CASINO_HOME at the mount:
#
#     docker run --rm -i \
#         -v "$HOME/bin/CASINO:/opt/CASINO:ro" -e CASINO_HOME=/opt/CASINO \
#         -e CASINO_ARCH=linuxpc-gcc-parallel.openmpi \
#         -v "$PWD:/work" -u "$(id -u):$(id -g)" casino-mcp

FROM python:3.12-slim AS builder

# The dependency set is one package (the MCP SDK) and there is no lockfile, so the install
# is a single step: splitting it into a cached dependency layer would only duplicate the
# version constraint that pyproject.toml already carries.
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir .


FROM python:3.12-slim AS runtime

# Run as an unprivileged user -- an MCP stdio server needs no elevated privileges, and
# Glama's automated checks prefer it. The home directory matters: the job registry lands in
# $XDG_STATE_HOME, which defaults to ~/.local/state/casino-mcp.
RUN useradd --create-home --shell /bin/bash mcp
WORKDIR /app

# pip installed the package into the virtual environment, so the environment is the whole
# payload -- no source tree, caches, tests or examples come along.
COPY --from=builder --chown=mcp:mcp /app/.venv ./.venv

ENV PATH="/app/.venv/bin:$PATH" \
    HOME="/home/mcp" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER mcp

# MCP stdio protocol: stdin/stdout are the transport, logs go to stderr. `serve` is the
# default, but the other subcommands stay reachable -- `docker run casino-mcp config` is
# the first thing to try when a tool call refuses.
ENTRYPOINT ["casino-mcp"]
CMD ["serve"]
