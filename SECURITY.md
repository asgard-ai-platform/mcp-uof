# Security Policy

## Supported Versions

This repository is currently pre-1.0. Security fixes are applied to the latest `main` branch.

## Reporting a Vulnerability

Do not open a public issue for secrets, credentials, tokens, authorization bypasses, or data exposure reports.

Report privately to the repository maintainers through your normal security contact channel. Include:

- A clear description of the issue.
- A minimal reproduction if possible.
- Affected files, commands, or configuration.
- Whether any credential, token, hostname, user data, or customer data was exposed.

## Credential Handling

- Never commit `.env`, UOF accounts or passwords, generated reports, or local credential/cookie files.
- By default the MCP server signs in through the browser: `uof_custom_login` runs a temporary reverse proxy bound to `127.0.0.1` (random port, one-time token, Host check, auto-shutdown) so the user logs in on UOF's own page. The login form is relayed as-is through the proxy, so the
  password does transit the server process in memory — it is never parsed, logged, persisted, or
  returned to the AI, and never stored in a config file. Only the account field is read, to label
  which identity a stored session belongs to.
- The resulting session is stored under `$UOF_SESSION_DIR` (default `~/.uof`) with `0600` permissions. `UOF_SESSION_FILE` may select a fixed filename for external session handoff. **That file is a replayable session cookie — treat it like a password.** Revoke it with `uof_custom_logout`, or set `UOF_SESSION_PERSIST=false` to keep the session in memory only.
- An unattended fallback logs in with the plaintext `UOF_ACCOUNT` / `UOF_PASSWORD` from `.env` (posted to UOF's `Login.aspx` over HTTPS). Keep `.env` out of version control and prefer browser sign-in wherever a human is present.
- Use `.env.example` for placeholders only.
- Treat manual and end-to-end test outputs as sensitive unless reviewed and sanitized.

## Deployment Notes

- `UOF_SSL_VERIFY=false` is only intended for test environments with incomplete or self-signed certificates.
- Use strict SSL verification in production.
