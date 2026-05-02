# Changelog

All notable user-facing changes should be recorded here.

This project does not yet promise stable semver compatibility for every API;
the public room APIs and the `hmctl` CLI are the intended compatibility
surface.

## Unreleased

- Added repository hygiene docs: contributing guide, security policy, changelog,
  and GitHub issue templates.
- Added README badges and a shorter top-level capability summary.

## 0.3.6 - 2026-05-02

- Published the public CLI package as `hmctl` on PyPI.
- Kept `hivemind` as a backwards-compatible CLI alias.
- Added self-serve signup with zero starting credit.
- Added admin-minted credit codes for tenant top-ups.
- Added tenant balance and admin billing commands.
- Added `hmctl doctor` for profile, service, billing, attestation, and room
  checks.
- Added admin tenant key reset and clean-start repair workflow.
- Standardized docs on room-native APIs and current artifact paths.
