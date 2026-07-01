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

- Never commit `.env`, UOF accounts or passwords, RSA key pairs, generated tokens, generated reports, or local credential files.
- The MCP server operates with the plaintext `UOF_ACCOUNT` / `UOF_PASSWORD` from `.env`; the code RSA-encrypts them before sending. Keep `.env` out of version control.
- Use `.env.example` for placeholders only.
- Treat manual and end-to-end test outputs as sensitive unless reviewed and sanitized.

## Deployment Notes

- `UOF_VERIFY_SSL=false` is only intended for test environments with incomplete or self-signed certificates.
- Use strict SSL verification in production.
- The SSE demo token mechanism (`MCP_SSE_TEST_TOKEN`) is for local testing. Replace it with real token verification before deploying an exposed service.
