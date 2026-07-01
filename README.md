# MCP UOF

[![PyPI version](https://img.shields.io/pypi/v/mcp-uof.svg)](https://pypi.org/project/mcp-uof/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcp-uof.svg)](https://pypi.org/project/mcp-uof/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![GitHub stars](https://img.shields.io/github/stars/asgard-ai-platform/mcp-uof.svg)](https://github.com/asgard-ai-platform/mcp-uof/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/asgard-ai-platform/mcp-uof.svg)](https://github.com/asgard-ai-platform/mcp-uof/issues)
[![GitHub last commit](https://img.shields.io/github/last-commit/asgard-ai-platform/mcp-uof.svg)](https://github.com/asgard-ai-platform/mcp-uof/commits/main)
[![MCP compatible](https://img.shields.io/badge/MCP-compatible-blue.svg)](https://modelcontextprotocol.io/)

[繁體中文](README.zh-TW.md)

An open-source [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that wraps UOF (U-Office Force) SOAP/ASMX services into AI-callable tools for workflow automation.

Built for Claude Code, Claude Desktop, VS Code, and any MCP-compatible client. It lets AI agents query workflow forms, inspect form schemas, submit forms, track workflow progress, and close workflow tasks through natural language.

## What This Does

- **12 ready-to-use tools** for UOF workflow operations, including authentication checks, form discovery, schema lookup, workflow preview, form submission, task status, task result, and task closure.
- **MCP server** over stdio for local AI clients, plus an optional SSE server for HTTP integrations.
- **Tool-first interface**: users call the same tools regardless of whether the implementation uses SOAP/PublicAPI or browser automation internally.
- **Single identity model**: one server process represents one UOF account configured through environment variables.
- **SOAP and web support**: SOAP is used where PublicAPI supports the operation; Playwright-backed web automation fills gaps such as form listing and selected custom form submission flows.

## API Reference

This project targets UOF first-generation SOAP/ASMX services and selected web flows.

- Authentication: UOF account/password encrypted with the configured RSA public key for SOAP token access; web flows reuse a browser session.
- Base URL: configured with `UOF_BASE_URL`, for example `https://your-uof-domain.com/VirtualPath`.
- Required UOF settings: see [docs/configuration.md](docs/configuration.md).

## Quick Start

### Install

```bash
pip install mcp-uof
```

Or use uvx:

```bash
uvx --from mcp-uof mcp-uof
```

For local development from source:

```bash
git clone https://github.com/asgard-ai-platform/mcp-uof.git
cd mcp-uof
uv sync
cp .env.example .env
uv run playwright install chromium
```

Set the required environment variables:

```bash
export UOF_BASE_URL=https://your-uof-domain.com/VirtualPath
export UOF_APP_NAME=your_app_name
export UOF_RSA_PUBLIC_KEY=your_rsa_public_key_base64
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
  -e UOF_APP_NAME=your_app_name \
  -e UOF_RSA_PUBLIC_KEY=your_rsa_public_key_base64 \
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
        "UOF_APP_NAME": "your_app_name",
        "UOF_RSA_PUBLIC_KEY": "your_rsa_public_key_base64",
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
        "UOF_APP_NAME": "your_app_name",
        "UOF_RSA_PUBLIC_KEY": "your_rsa_public_key_base64",
        "UOF_ACCOUNT": "your_account",
        "UOF_PASSWORD": "your_password"
      }
    }
  }
}
```

See [docs/integration.md](docs/integration.md) and [examples/](examples/) for more client configuration examples.

## Tools (12)

All tool names use the `uof_custom_` prefix.

| Domain | Tools |
|--------|-------|
| System | `check_auth` |
| WKF Workflow | `get_form_list`, `get_external_form_list`, `query_forms`, `get_form_structure`, `get_form_structure_by_id`, `preview_workflow`, `apply_form`, `get_task_data`, `get_task_result`, `terminate_task`, `sign_next` |

Important behavior and constraints:

- UOF first-generation PublicAPI does not provide an inbox or pending-task list API. Users must provide a TaskId from the UOF UI or notification email.
- UOF does not provide a general per-step approval API. Single-step free-flow approvals can be represented through `terminate_task` with `Adopt` or `Reject` when used by the current signer.
- `terminate_task` can overwrite already-closed results at the API layer; this server checks task status first and blocks repeated closure.
- Some form submissions use web automation when SOAP intermediary fields cannot represent the full form body.

See [docs/tools.md](docs/tools.md) for full tool specs, role model, examples, and operational boundaries.

## Project Structure

```text
mcp-uof/
├── src/mcp_uof/                 # MCP server, auth, routing, SOAP/web backends
├── docs/                        # Architecture, configuration, integration, tools, testing
├── examples/                    # Claude Desktop and VS Code MCP config examples
├── scripts/                     # RSA key and WSDL helper scripts
├── tests/                       # smoke / e2e / mounted test layers
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
uv run python tests/run.py e2e
uv run python tests/run.py mounted
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/testing.md](docs/testing.md) for development and testing guidelines.

## License

MIT
