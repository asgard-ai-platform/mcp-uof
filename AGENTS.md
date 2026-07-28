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
- `src/mcp_uof/auth/` — authentication for UOF web session login.
  - `base.py` defines the provider contract, the tool-entry auth gate, and the two failure messages.
  - `session.py` resolves the session across its three sources (stored file, env credentials, browser login).
  - `browser_login.py` runs the localhost reverse-proxy sign-in flow.
  - `store.py` persists the cookie jar under `UOF_SESSION_DIR` (default `~/.uof`) with `0600` permissions.
- `src/mcp_uof/ops/` — operation layer used by MCP tools.
  - `base.py` defines the backend interface.
  - `router.py` contains the tool-to-backend binding table.
  - `http_web/` implements UOF web automation as composed runtime, form, detail, dialog, task, and session-lifecycle modules.
- `src/mcp_uof/_log.py` — shared stderr logging helper for stdio-safe diagnostics.

## Test layout

- `tests/run.py` — unified test runner.
- `tests/smoke/` — offline checks for imports, tool bindings, session store, browser-login proxy, and auth priority.
- `tests/mounted/` — real mounted MCP tests using a stdio subprocess and live UOF environment settings.
- `tests/_common.py` — shared test helpers.
- `tests/_fake_uof.py` — fake UOF upstream used by the offline authentication tests.

## Documentation map

- `docs/architecture.md` — runtime architecture and identity model.
- `docs/configuration.md` — environment variables.
- `docs/design.md` — implementation design and tool-to-UOF endpoint mapping.
- `docs/tools.md` — public MCP tool reference.
- `docs/testing.md` and `tests/README.md` — test strategy and test discipline.
- `docs/integration.md` — Claude Desktop and VS Code MCP setup examples.
- `docs/form-requirements.md` — UOF form configuration requirements.
- `docs/example-session.md` — example end-to-end usage flow.
