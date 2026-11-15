# Security

## Reporting a vulnerability

Write to **security@korely.ai**, the address in
[korely.ai/.well-known/security.txt](https://korely.ai/.well-known/security.txt).
Do not open an issue or a pull request: this repository is public, and
either is read by anybody before a fix exists.

Say, as far as you know it:

- what an attacker can do, and what they need first (an API key, an operator
  login, a network position, nothing);
- the steps that show it, with the requests and their answers;
- the version (`curl -s https://your-domain/health` says it, and so does
  `VERSION`), and whether it is an install of your own or the Cloud
  (`api.korely.ai`), which runs the same code.

Leave real people's data out of the report: an end user's memories, facts or
id, an API key, a model provider's key. A placeholder shows the bug as well.
If a key of yours was exposed while you found it, revoke it first (the API
keys page, or `manage revoke-key`).

We acknowledge a report within 5 business days and give a first assessment
within 14, as security.txt says, and keep you told until the fix ships. There
is no paid bug bounty; a reporter who wants to be named is named.

## What is covered

The service in this repository: the API, the MCP server, the dashboard, the
worker, the files in `deploy/` and the images built from it, on an install of
one's own and on the Cloud. The client libraries (`korely-memory` on PyPI and
npm) go to the same address.

## Fixes

A fix ships in the next release. From 0.1.41 on, every release's entry in
[CHANGELOG.md](../CHANGELOG.md) has a `### Security` section: the fixes it
carries, each with what to do before and after upgrading or the line of the
entry that says it, or that it carries none. Before 0.1.41 that section is in
a few entries only (0.1.18, 0.1.19, 0.1.20, 0.1.26, 0.1.28), and a fix of an
older release may sit under its entry's other headings.
There are no maintenance branches: an install gets a fix by upgrading to the
release that carries it (README, "Keeping it running").
