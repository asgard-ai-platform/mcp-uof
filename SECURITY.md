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
- The MCP server logs in with the plaintext `UOF_ACCOUNT` / `UOF_PASSWORD` from `.env` (posted to UOF's `Login.aspx` over HTTPS, maintaining a cookie session). Keep `.env` out of version control.
- Use `.env.example` for placeholders only.
- Treat manual and end-to-end test outputs as sensitive unless reviewed and sanitized.

## Deployment Notes

- `UOF_SSL_VERIFY=false` is only intended for test environments with incomplete or self-signed certificates.
- Use strict SSL verification in production.
