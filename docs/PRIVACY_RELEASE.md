# Privacy and release gate

The release gate is deliberately local, deterministic, and fail-closed. It
uses the Python standard library for file and archive inspection. Git is used
only to ask the repository for its staged and reachable objects; no secret
scanner, network service, shell pipeline, or private infrastructure is
required.

The gate reports no matching excerpt. Every finding contains exactly:

- a rule identifier;
- a repository- or archive-relative path and line number, unless the path
  itself is sensitive, in which case only `@redacted-path/sha256:<digest>` is
  emitted;
- a truncated SHA-256 fingerprint of the matched value.

The fingerprint is evidence for comparing repeated scans, not a safe way to
publish the original value. Gate output should still be treated as build
metadata and retained only as long as needed.

## What is blocked

The built-in rules detect:

- Unix and Windows user-home paths;
- email addresses, except documentation addresses under `example.invalid`
  and `noreply` at the reserved example domains, plus GitHub's
  `users.noreply.github.com` privacy addresses;
- international phone numbers and Telegram-shaped bot tokens, links, invite URLs,
  and ID assignments, except the three exact reviewed public project-resource
  URLs pinned in the scanner;
- private keys, certificates, common provider-token shapes, credential URLs,
  and secret assignments;
- runtime databases, logs, receipts, sessions, worktrees, credential files,
  and private tool-state directories;
- binary, unreadable, special, or symbolic-link entries.

The scanner also bounds file count, individual size, and total bytes. A limit
breach is a failure, not a skipped file.

The only binary exception is the reviewed hero image at
`docs/assets/hermes-codex-operator-hero.png`. Its path and SHA-256 are pinned
in `scripts/privacy_gate.py`; changed bytes or a copy at another path fail
closed until the replacement is reviewed and repinned.

## Private denylist

Built-in patterns cannot know private project, team, host, or profile names.
Keep those markers in a separate denylist that is never committed:

```bash
PRIVATE_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-codex-operator"
mkdir -p "$PRIVATE_CONFIG_DIR"
chmod 700 "$PRIVATE_CONFIG_DIR"
cp .privacy.example "$PRIVATE_CONFIG_DIR/hermes-codex-operator-private-markers.txt"
chmod 600 "$PRIVATE_CONFIG_DIR/hermes-codex-operator-private-markers.txt"
```

Replace the commented placeholders with one marker per line. Blank lines and
comments are ignored. Matching applies Unicode NFKC normalization followed by
case folding, so capitalization and compatibility Unicode characters do not
bypass the check.

The gate rejects a denylist that:

- is inside the repository;
- is a symlink or non-regular file;
- is not exactly mode `0600` on POSIX;
- is unreadable, binary, empty, oversized, or contains a marker shorter than
  four normalized characters.

Do not print or upload the denylist. Pass it directly as an argv value; the
gate never includes its path or terms in findings.

## Scan modes

Scan the current source tree, excluding only VCS and disposable build/cache
directories:

```bash
python3 scripts/privacy_gate.py . --mode tree \
  --denylist "$PRIVATE_CONFIG_DIR/hermes-codex-operator-private-markers.txt"
```

Scan the exact blobs and modes in the Git index. A clean worktree copy cannot
hide a leak already staged:

```bash
python3 scripts/privacy_gate.py . --mode staged \
  --denylist "$PRIVATE_CONFIG_DIR/hermes-codex-operator-private-markers.txt"
```

Scan every reachable commit's metadata, every annotated tag's metadata, all
historical names, and every reachable blob. With no reachable commits this
mode succeeds after scanning zero objects:

```bash
python3 scripts/privacy_gate.py . --mode history \
  --denylist "$PRIVATE_CONFIG_DIR/hermes-codex-operator-private-markers.txt"
```

Build both standard distributions with pinned frontend/backend versions:

```bash
python3 -m pip install build==1.5.0
mkdir -p dist/raw
python3 -m build --sdist --wheel --outdir dist/raw
python3 scripts/normalize_sdist.py \
  dist/raw/hermes_codex_operator-0.0.1.tar.gz \
  --expected-root hermes_codex_operator-0.0.1 \
  --output dist/hermes_codex_operator-0.0.1.tar.gz
cp dist/raw/hermes_codex_operator-0.0.1-py3-none-any.whl dist/
```

Some sdist builders preserve the local account name in tar owner fields. The
normalizer performs an atomic, fail-closed rewrite with neutral `root:root`
ownership, one canonical archive root, collision-safe paths, fixed ordering,
modes, and timestamps, and no links or special members. It prints the resulting
SHA-256. Never publish or scan the raw pre-normalization sdist as the candidate
artifact. CI records the raw digest for provenance, installs the normalized
sdist, then installs the exact wheel as a separate smoke check. The root and
filename binding follows [PEP 625](https://peps.python.org/pep-0625/).

Scan both tar/sdist and ZIP/wheel formats without extracting them. Absolute
paths, traversal, links, special files, duplicate paths, local owner metadata,
unapproved binary entries, and decompression-size limits fail closed:

```bash
python3 scripts/privacy_gate.py . --mode release \
  --archive dist/hermes_codex_operator-0.0.1.tar.gz \
  --archive dist/hermes_codex_operator-0.0.1-py3-none-any.whl \
  --denylist "$PRIVATE_CONFIG_DIR/hermes-codex-operator-private-markers.txt"
```

`--mode` can be repeated. `--mode all` runs tree, staged, and history; it also
runs release when `--archive` is supplied. Running the script without a mode
keeps the development default of `tree`.

## Publication checklist

1. Work from the clean-room repository, never from a copied private history.
2. Review staged filenames and changes normally.
3. Run `tree`, `staged`, and `history` with the private denylist.
4. Build fresh sdist and wheel artifacts from the reviewed commit, then
   normalize the sdist owner metadata.
5. Run `release` against every exact artifact with the same denylist and retain
   their SHA-256 digests as provenance.
6. Run the unit tests in a clean environment.
7. Have a second reviewer inspect the public tree, refs, and archive.
8. Publish only the already-scanned artifacts and commit.

Deletion from the current branch is not remediation: rotate an exposed secret
and rebuild a clean history before publication.

## Planned second layer

A future release workflow should add Gitleaks as an independent second scanner.
That integration must pin one exact Gitleaks release and the downloaded
artifact's SHA-256 in a reviewed lock/config file; `latest`, floating action
tags, and unverified downloads are forbidden. Gitleaks is intentionally not a
dependency of this stdlib gate today. The local gate remains responsible for
private denylist terms, forbidden runtime state, Git metadata, and safe tar
structure even after that second layer is enabled.

## Boundary

Pattern matching reduces accidental disclosure; it cannot prove that text is
non-sensitive. Human review remains mandatory. The gate does not rewrite Git
history, rotate credentials, classify business information, or make an
untrusted archive safe to execute.
