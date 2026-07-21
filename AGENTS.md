# mcp-uof project structure

This repository contains a Python MCP server for UOF first-generation workflow operations.

## Top-level layout

- `src/mcp_uof/` — Python package source code.
- `docs/` — architecture, configuration, integration, testing, and tool reference documentation.
- `examples/` — sample MCP client configuration files.
- `tests/` — executable smoke and mounted test scripts.
- `.github/` — GitHub Actions workflows and issue templates.
- `.env.example` — environment variable template for local setup.
- `pyproject.toml` / `uv.lock` — package metadata and dependency lockfile.

## Source package layout

- `src/mcp_uof/server.py` — stdio MCP server entrypoint; registers the public `uof_custom_*` tools.
- `src/mcp_uof/auth/` — authentication provider code for UOF web session login.
- `src/mcp_uof/ops/` — operation layer used by MCP tools.
  - `base.py` defines the backend interface.
  - `router.py` contains the tool-to-backend binding table.
  - `http_web.py` implements UOF web automation with `httpx` and `lxml`.
- `src/mcp_uof/_log.py` — shared stderr logging helper for stdio-safe diagnostics.

## Test layout

- `tests/run.py` — unified test runner.
- `tests/smoke/` — offline checks for imports and tool bindings.
- `tests/mounted/` — real mounted MCP tests using a stdio subprocess and live UOF environment settings.
- `tests/_common.py` — shared test helpers.

## Documentation map

- `docs/architecture.md` — runtime architecture and identity model.
- `docs/configuration.md` — environment variables.
- `docs/design.md` — implementation design and tool-to-UOF endpoint mapping.
- `docs/tools.md` — public MCP tool reference.
- `docs/testing.md` and `tests/README.md` — test strategy and test discipline.
- `docs/integration.md` — Claude Desktop and VS Code MCP setup examples.
- `docs/form-requirements.md` — UOF form configuration requirements.
- `docs/example-session.md` — example end-to-end usage flow.
