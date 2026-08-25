# Repository instructions

- Keep the portable core independent of Telegram, Kanban, Codex Desktop UI,
  and private agent profiles.
- Never add real prompts, results, receipts, identifiers, screenshots, user
  paths, hostnames, credentials, or runtime databases.
- Use synthetic fixtures and temporary directories in tests.
- Preserve the invariant: an ambiguous dispatch is `UNKNOWN`, never an
  automatic retry.
- Treat App Server readback as authoritative; streamed text is not final proof.
- Verification commands are argv arrays and must never use a shell.
- Keep safe permissions as defaults. Broader access must be explicit caller
  configuration, never repository policy.
- Run the unit suite and privacy gate before claiming completion.
