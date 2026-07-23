# MCP UOF

[![PyPI version](https://img.shields.io/pypi/v/mcp-uof.svg)](https://pypi.org/project/mcp-uof/) [![Python versions](https://img.shields.io/pypi/pyversions/mcp-uof.svg)](https://pypi.org/project/mcp-uof/) [![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT) [![GitHub stars](https://img.shields.io/github/stars/asgard-ai-platform/mcp-uof.svg)](https://github.com/asgard-ai-platform/mcp-uof/stargazers) [![GitHub issues](https://img.shields.io/github/issues/asgard-ai-platform/mcp-uof.svg)](https://github.com/asgard-ai-platform/mcp-uof/issues) [![GitHub last commit](https://img.shields.io/github/last-commit/asgard-ai-platform/mcp-uof.svg)](https://github.com/asgard-ai-platform/mcp-uof/commits/main) [![MCP compatible](https://img.shields.io/badge/MCP-compatible-blue.svg)](https://modelcontextprotocol.io/)

[繁體中文](README.zh-TW.md)

An open-source [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that turns UOF (U-Office Force) workflow operations into AI-callable tools, driven entirely through httpx web automation.

Built for Claude Code, Claude Desktop, VS Code, and any MCP-compatible client. It lets AI agents query workflow forms, inspect form schemas, submit forms, track workflow progress, sign off, and close workflow tasks through natural language.

## What This Does

- **17 exposed tools** for UOF workflow operations. `preview_workflow` and `get_external_form_list` currently return capability guidance rather than live data.
- **MCP server** over stdio for local AI clients.
- **Tool-first interface**: users call the same tools without ever choosing a mechanism — how each tool talks to UOF is an internal, developer-time decision.
- **Single identity model**: one server process represents one UOF account configured through environment variables.
- **httpx web automation**: operations use HTTPS requests (`httpx` + `lxml`) against UOF's `aspx`/`ashx` endpoints, without a browser runtime. On Alpine Linux or musl, ensure binary wheels or native build dependencies are available.

## API Reference

This project targets UOF first-generation web flows, driven over httpx.

- Authentication: UOF account/password posted to `Login.aspx`, maintaining a cookie session.
- Base URL: configured with `UOF_BASE_URL`, for example `https://your-uof-domain.com/VirtualPath`.
- Required UOF settings: see [docs/configuration.md](docs/configuration.md).

## Quick Start

### Install From Source

The current PyPI release (`0.1.7`) contains the previous SOAP-based implementation and does not
match this documentation. Until a new version is published, install and run the current source:

```bash
git clone https://github.com/asgard-ai-platform/mcp-uof.git
cd mcp-uof
uv sync
cp .env.example .env
```

Set the required environment variables:

```bash
export UOF_BASE_URL=https://your-uof-domain.com/VirtualPath
export UOF_ACCOUNT=your_account
export UOF_PASSWORD=your_password
```

### Use with Claude Code

Add the server via the Claude CLI:

```bash
claude mcp add --transport stdio uof -- mcp-uof
```

Or with environment variables inline:

```bash
claude mcp add --transport stdio uof \
  -e UOF_BASE_URL=https://your-uof-domain.com/VirtualPath \
  -e UOF_ACCOUNT=your_account \
  -e UOF_PASSWORD=your_password \
  -- mcp-uof
```

If you clone the repo locally, run it through `uv`:

```bash
claude mcp add --transport stdio uof -- uv --directory /absolute/path/to/mcp-uof run mcp-uof
```

### Use with Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "uof": {
      "command": "mcp-uof",
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath",
        "UOF_ACCOUNT": "your_account",
        "UOF_PASSWORD": "your_password"
      }
    }
  }
}
```

Or with a local checkout:

```json
{
  "mcpServers": {
    "uof": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath",
        "UOF_ACCOUNT": "your_account",
        "UOF_PASSWORD": "your_password"
      }
    }
  }
}
```

See [docs/integration.md](docs/integration.md) and [examples/](examples/) for more client configuration examples.

## Tools (17)

All tool names use the `uof_custom_` prefix.

| Domain | Tools |
| --- | --- |
| System | `check_auth` |
| WKF Workflow | `get_form_list`, `get_external_form_list`, `query_forms`, `get_pending_sign_list`, `search_users`, `get_form_structure`, `get_form_structure_by_id`, `get_dialog_structure`, `search_dialog_options`, `operate_dialog`, `preview_workflow`, `apply_form`, `get_task_data`, `get_task_result`, `sign_next`, `terminate_task` |

Important behavior and constraints:

- `get_pending_sign_list` returns every form awaiting the current identity's signature (with TaskId/SiteId/NodeSeq), sourced from the Homepage pending-sign widget. `query_forms` is a different set — the forms you submitted or signed, by date range (`query_mode` = `apply`/`sign`). Ask "what do I need to sign?" → `get_pending_sign_list`.
- Composite fields (line items, vendor pickers, expense details) live inside **dialogs**. Use `get_dialog_structure` to see a dialog field's inner controls, `search_dialog_options` to look up real picker candidates (never fabricate codes), and pass them into `apply_form` via the `_lookups` / `_fill_before` / `_press_after` / `_rows` reserved keys. `operate_dialog` is a probe only — it cannot accumulate rows.
- `sign_next` performs approval for the current pending step and can close the flow or route to a designated next signer. It does not accept a signing comment; return, parallel/countersign, and fixed-flow stepping still require the Web UI.
- `terminate_task` closes a task: `Cancel` voids an in-flight form (via the web recall page), `Adopt`/`Reject` approve/reject through the web sign flow. It checks task status first and blocks repeated closure of an already-closed task.
- `preview_workflow` (flow simulation) is not available over httpx and directs the user to the Web UI; you can still submit directly with `apply_form` and inspect the real signing route afterward with `get_task_result`.
- `apply_form` always submits as the configured `UOF_ACCOUNT`. Its `applicant_account` and `first_signer_account` parameters are currently retained for interface compatibility but do not change the submitted identity or routing.

See [docs/tools.md](docs/tools.md) for full tool specs, role model, examples, and operational boundaries.

## Project Structure

```text
mcp-uof/
├── src/mcp_uof/                 # MCP server, auth (web session), routing, httpx web backend
├── docs/                        # Architecture, configuration, integration, tools, testing
├── examples/                    # Claude Desktop and VS Code MCP config examples
├── tests/                       # smoke / mounted test layers
├── .env.example                 # Environment variable template
├── README.zh-TW.md              # Traditional Chinese README
└── pyproject.toml
```

## Development

```bash
uv sync
uv run python tests/run.py smoke
uv run python -m compileall src tests
```

Tests that connect to a real UOF test environment require `.env`:

```bash
uv run python tests/run.py mounted
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/testing.md](docs/testing.md) for development and testing guidelines.

## License

MIT
