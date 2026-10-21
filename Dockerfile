# Context Guardian MCP server (stdio), for MCP hosts that build a container (Glama's build check, for example).
#
#   docker build -t context-guardian-mcp .
#   docker run -i --rm context-guardian-mcp
#
# The image runs only the MCP server. get_proxy_status reaches a proxy on the host only when the container can
# (e.g. --network host, or GUARDIAN_HOST=host.docker.internal); list_spans / search_spans / read_span need the archive
# mounted: -v /home/user/project/logs/guardian_spans:/spans -e GUARDIAN_SPAN_DIR=/spans.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY context_guardian.py cg_doctor.py cg_configure.py cg_recall_cli.py cg_mcp.py ./
RUN pip install --no-cache-dir . \
 && useradd --create-home --uid 10001 guardian
USER guardian
ENV GUARDIAN_SPAN_DIR=/spans

ENTRYPOINT ["context-guardian-mcp"]
