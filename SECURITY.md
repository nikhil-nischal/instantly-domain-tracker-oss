# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Instead, report
them privately to the maintainers (for example, via GitHub's "Report a
vulnerability" feature on the repository's Security tab).

Include a description of the issue, steps to reproduce, and the potential
impact. We will acknowledge the report and coordinate a fix and disclosure.

## Handling secrets

This project reads an Instantly API key and a dashboard password from
environment variables (see `.env.example`). Never commit a filled-in `.env`,
`data/users.json`, or any file under `data/` — these are gitignored by design.

If you believe a key was exposed, revoke and rotate it in your Instantly
account immediately.
