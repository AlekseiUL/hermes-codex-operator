# Security policy

Hermes Codex Operator is pre-alpha. Only the latest tagged release will receive
security fixes until a stable support policy is published.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/AlekseiUL/hermes-codex-operator/security/advisories/new).
Do not include credentials, private prompts, personal data, or an exploit
against a real project in a public issue.

Include:

- affected version and platform;
- the smallest synthetic reproduction;
- expected and observed behavior;
- security impact;
- whether any secret or private data may have been exposed.

## Security boundary

The operator validates contracts, records task identity, constrains evidence to
an allowlisted workspace, verifies changed paths, and avoids shell parsing. It
does not replace Codex sandboxing, operating-system permissions, or human
authorization.

Verification argv is trusted caller input and runs outside the Codex sandbox as
the operator's OS user. `shell=false` avoids shell interpretation but does not
confine the executable or prevent writes outside the workspace.

The following are never implied by a WorkContract:

- permission to publish, deploy, push, or open a pull request;
- permission to message a third party;
- permission to mutate another workspace;
- permission to broaden Codex sandbox or approval settings;
- permission to copy private team context into a prompt or receipt.

## Secrets and private data

- Never commit tokens, credentials, private keys, chat identifiers, raw prompts,
  model results, receipts from real work, runtime databases, or user-home paths.
- Use synthetic fixtures and disposable workspaces for tests.
- Keep the private denylist outside the repository with mode `0600`.
- Run tree, staged, history, sdist, and wheel privacy gates before publishing.
- Treat privacy-gate output as build metadata even though it contains only
  fingerprints, not matching excerpts.

See [the threat model](docs/THREAT_MODEL.md) and
[the release procedure](docs/PRIVACY_RELEASE.md).
