# Contributing

Thanks for improving Hivemind. This project is security-sensitive: small
changes to room trust, billing, tenant isolation, or sandbox behavior can have
real product impact. Keep changes focused and include verification.

## Development Setup

Requirements:

- Python 3.11+
- `uv`
- Docker
- Postgres for local integration work

Install dependencies:

```bash
uv sync
```

Run the local quickstart:

```bash
./scripts/quickstart.sh
```

Install the CLI from the checkout:

```bash
uv tool install --editable .
hmctl --help
```

The package also installs `hivemind` as a backwards-compatible CLI alias.

## Checks

Before opening a PR, run:

```bash
uv run ruff check hivemind tests scripts
uv run pytest -q
uv build
```

For narrow changes, targeted tests are fine while iterating, but run the full
suite before asking for review when behavior, APIs, billing, auth, or sandbox
code changed.

## Pull Requests

Good PRs usually include:

- a short problem statement;
- a concise summary of the implementation;
- tests or a clear reason tests were not added;
- commands run locally;
- screenshots or CLI output for user-facing UX changes.

Prefer the existing architecture and helper APIs over new abstractions. Avoid
unrelated refactors in behavior PRs.

## Security And Privacy Notes

Be especially careful with:

- tenant key handling and one-time plaintext key responses;
- room invite tokens and capability-token constraints;
- attestation, TLS pinning, compose-hash governance, and bypass flags;
- billing holds, releases, and negative-balance enforcement;
- sandbox egress, artifact visibility, and sealed agent source handling.

Do not include real tenant keys, admin keys, private room links, or copied
secrets in issues, PRs, logs, or tests.
