# Security Policy

Hivemind handles tenant isolation, room invite tokens, billing, sandboxed
agents, and TEE attestation. Please report security issues privately.

## Reporting A Vulnerability

Use GitHub private vulnerability reporting for this repository when available:

```text
https://github.com/teleport-computer/hivemind-core/security/advisories/new
```

If private reporting is unavailable, contact a project maintainer directly
before filing a public issue. Do not disclose exploit details, tenant keys,
admin keys, private room links, or live service credentials in public channels.

Useful report details:

- affected commit, version, service URL, or deployment mode;
- exact impact and what boundary is crossed;
- minimal reproduction steps;
- whether credentials, tenant data, room data, or artifacts may be exposed;
- logs with secrets redacted.

## Scope

Security-sensitive areas include:

- tenant authentication and key rotation;
- credit enforcement and billing holds/releases;
- room invite capability tokens;
- sealed agent source and room-vault data;
- sandbox networking, filesystem, and bridge APIs;
- dstack attestation, TLS pinning, compose-hash verification, and trust bypasses.

## Supported Versions

The actively maintained version is the latest `main` branch and the latest
published `hmctl` package on PyPI. Older internal or pre-room APIs are not a
supported public compatibility surface.

## Secret Handling

If a secret is accidentally exposed, rotate it immediately. This includes:

- `hmk_...` tenant API keys;
- `hmq_...` room/capability tokens;
- `hmcc_...` credit codes before redemption;
- `HIVEMIND_ADMIN_KEY`;
- PyPI, GitHub, Cloudflare, RPC, or deploy credentials.
