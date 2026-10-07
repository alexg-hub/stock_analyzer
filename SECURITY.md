# Security policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue. Use GitHub's
**Report a vulnerability** button on the
[Security tab](https://github.com/alexg-hub/stock_analyzer/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker could do with
it. This is a personal project maintained on a best-effort basis: I will
acknowledge a report as soon as I can and fix confirmed issues in `main`.

## Supported versions

There are no releases. Only the current `main` branch is supported.

## In scope

- Anything that could expose a secret: the Discord webhook or the SEC contact
  loaded from `.env`, or a secret ending up in a log, a report or the repository.
- A way to post to Discord, write `config.json` or record into the signal
  history without the safeguards: tools that can post default to `send=False`,
  config writers preview a diff until `confirm=True`, and the writers refuse
  the Discord settings outright.
- A way for the MCP server or its tools to read or write outside the project
  folder, or to run arbitrary commands.
- Dependencies with known vulnerabilities that affect how this project uses them.

## Out of scope

- Accuracy of third-party data (Yahoo Finance, SEC EDGAR, Wikipedia, Google
  News) or of any signal, score or verdict.
- Trading losses. This is a research tool, not investment advice; see the
  README.
- Issues that need an attacker who can already edit your local `config.json`,
  `.env` or source files.

## Design facts that may help

- There is no brokerage-account code. Nothing reads positions, balances or
  orders, and the portfolio is a virtual ledger that cannot place a trade.
- Secrets live only in `.env`, which is git-ignored. `config.json` holds no
  credentials, and `tests/test_secrets.py` checks it.
- The analyzer never starts an AI model; `tests/test_no_model.py` checks it.
- The MCP server runs locally over stdio for the Claude client that launched
  it. It opens no network port.
