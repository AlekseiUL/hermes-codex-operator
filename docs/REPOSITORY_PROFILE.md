# Repository profile

This file separates verified GitHub state from recommended maintainer settings.
Recommendations are not claims that a setting is already enabled.

## Identity

- **Product name:** Hermes Codex Operator
- **Repository slug:** `hermes-codex-operator`
- **Python distribution:** `hermes-codex-operator`
- **CLI:** `hermes-codex`
- **Tagline:** Talk to Hermes. Ship with Codex.
- **Owner:** [AlekseiUL](https://github.com/AlekseiUL)
- **Repository:** [AlekseiUL/hermes-codex-operator](https://github.com/AlekseiUL/hermes-codex-operator)

## GitHub description

> A local-first operator that turns plain-language ideas into tracked, verified
> Codex work—planned by Hermes, executed by Codex, and explained back to you.

## Verified public settings — 2026-08-26

- visibility: public;
- default branch: `main`;
- license detected by GitHub: MIT;
- Issues: enabled;
- Projects, Wiki, and Discussions: disabled;
- private vulnerability reporting: enabled;
- branch protection/ruleset: not enabled at the time of this audit.

Topics:

`ai-agent`, `codex-app-server`, `codex-cli`, `developer-tools`, `hermes-agent`,
`local-first`, `openai-codex`, `python`, `task-lifecycle`

## Recommended maintainer settings

Before accepting outside contributions or publishing the next tag:

- require pull requests for `main`;
- require the `CI` workflow and current branches before merge;
- block force pushes and branch deletion after the privacy rewrite is complete;
- keep private vulnerability reporting enabled;
- enable secret scanning and push protection when the repository/account tier
  makes them available;
- keep GitHub Actions pinned to full commit SHAs;
- leave unused GitHub features disabled until they have an owner.

## Social preview

Use `docs/assets/hermes-codex-operator-hero.png`. The privacy gate pins its
exact path and digest. The image contains no personal data, screenshots, or
visible product text, so normal GitHub cropping does not hide required
information.

## Next tagged release

The latest published tag is `v0.0.1`. Before publishing `v0.1.0`:

1. choose the exact candidate commit;
2. run tests, package smoke, documentation checks, live Codex qualification,
   and every privacy mode against that candidate;
3. independently inspect tree, reachable history, and release artifacts;
4. enable the recommended `main` protection settings;
5. publish only the already-scanned commit and artifacts.
