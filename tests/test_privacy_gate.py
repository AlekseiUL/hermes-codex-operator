from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "privacy_gate.py"
SPEC = importlib.util.spec_from_file_location("operator_privacy_gate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
privacy_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = privacy_gate
SPEC.loader.exec_module(privacy_gate)


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_git(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Synthetic Maintainer")
    _git(root, "config", "user.email", "maintainer@example.invalid")


def _add_tar_bytes(bundle: tarfile.TarFile, name: str, content: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mtime = 0
    member.uid = 0
    member.gid = 0
    member.uname = "root"
    member.gname = "root"
    bundle.addfile(member, io.BytesIO(content))


def _render_report(report: object) -> str:
    return "\n".join(
        privacy_gate.render_finding(item) for item in getattr(report, "findings")
    )


class TreeScanTests(unittest.TestCase):
    def test_clean_tree_and_documentation_emails_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(
                root / "README.md",
                "\n".join(
                    (
                        "contact@example.invalid",
                        "noreply@example.com",
                        "123456+synthetic@users.noreply.github.com",
                        "plain synthetic documentation",
                        "",
                    )
                ),
            )
            report = privacy_gate.scan_tree(root)
            self.assertTrue(report.ok, report.findings)

    def test_allows_only_exact_reviewed_public_telegram_urls(self) -> None:
        allowed = (
            "https://t.me/Sprut_AI",
            "https://t.me/+eH-qNIDmud8zNDZi",
            "https://t.me/tribute/app?startapp=sJyg",
        )
        blocked = (
            "".join(("https://t.me/", "Private_Channel")),
            "".join(("https://t.me/+", "PrivateInvite123")),
            "".join(("https://t.me/", "tribute/app?startapp=", "other")),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "public.md", "\n".join(allowed) + "\n")
            clean = privacy_gate.scan_tree(root)
            self.assertTrue(clean.ok, clean.findings)

            _write(root / "private.md", "\n".join(blocked) + "\n")
            report = privacy_gate.scan_tree(root)
            telegram_findings = [
                item for item in report.findings
                if item.rule_id == "TELEGRAM_IDENTIFIER"
            ]
            self.assertEqual(len(telegram_findings), len(blocked))

    def test_detects_sensitive_content_without_retaining_excerpt(self) -> None:
        home = "".join(("/Us", "ers/pr", "ivate-person/project"))
        email = "".join(("person", "@private", ".test"))
        phone = "".join(("+995", " 555 ", "12 34 56"))
        telegram = "".join(("123456789", ":AA", "abcdefghijklmnopqrstuvwxzy123456"))
        private_key = "".join(("-----BEGIN ", "OPENSSH PRIVATE ", "KEY-----"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leak = "\n".join((home, email, phone, telegram, private_key))
            _write(root / "notes.txt", leak)
            report = privacy_gate.scan_tree(root)
            rules = {finding.rule_id for finding in report.findings}
            self.assertTrue(
                {"HOME_PATH", "EMAIL", "PHONE", "TELEGRAM_IDENTIFIER", "KEY_MATERIAL"}
                <= rules
            )
            rendered = "\n".join(privacy_gate.render_finding(item) for item in report.findings)
            for raw_value in (home, email, phone, telegram, private_key):
                self.assertNotIn(raw_value, rendered)

    def test_rejects_runtime_files_binary_files_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root = Path(directory)
            forbidden_name = "state" + ".sq" + "lite"
            _write(root / forbidden_name, "synthetic\n")
            _write(root / "image.bin", b"abc\0def")
            if hasattr(os, "symlink"):
                Path(root, "external").symlink_to(Path(other), target_is_directory=True)
            report = privacy_gate.scan_tree(root)
            rules = {finding.rule_id for finding in report.findings}
            self.assertIn("FORBIDDEN_RUNTIME_PATH", rules)
            self.assertIn("BINARY_FILE", rules)
            if hasattr(os, "symlink"):
                self.assertIn("SYMLINK_FORBIDDEN", rules)

    def test_allows_only_the_exact_pinned_public_image(self) -> None:
        approved_path = Path("docs/assets/hermes-codex-operator-hero.png")
        approved_bytes = (PROJECT_ROOT / approved_path).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / approved_path, approved_bytes)
            clean = privacy_gate.scan_tree(root)
            self.assertTrue(clean.ok, clean.findings)

            _write(root / approved_path, approved_bytes + b"changed")
            changed = privacy_gate.scan_tree(root)
            self.assertIn("BINARY_FILE", {item.rule_id for item in changed.findings})

            _write(root / approved_path, approved_bytes)
            _write(root / "docs/assets/unreviewed.png", approved_bytes)
            _write(root / "nested" / approved_path, approved_bytes)
            wrong_path = privacy_gate.scan_tree(root)
            self.assertIn("BINARY_FILE", {item.rule_id for item in wrong_path.findings})

    def test_sensitive_tree_path_is_redacted_in_every_finding(self) -> None:
        sensitive = "".join(("path-person", "@private", ".test"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / sensitive / "notes.txt", "synthetic\n")
            report = privacy_gate.scan_tree(root)
            self.assertIn("EMAIL", {item.rule_id for item in report.findings})
            rendered = _render_report(report)
            self.assertNotIn(sensitive, rendered)
            self.assertIn("@redacted-path/sha256:", rendered)

    def test_rejects_sqlite_sidecars_and_operation_lock_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "state.sqlite3-wal", "synthetic runtime state\n")
            _write(root / "state.sqlite3.locks" / "operation.lock", "synthetic\n")
            report = privacy_gate.scan_tree(root)
            findings = [
                item for item in report.findings
                if item.rule_id == "FORBIDDEN_RUNTIME_PATH"
            ]
            self.assertEqual(len(findings), 3)

    def test_unreadable_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "ordinary.txt"
            _write(target, "synthetic\n")
            original = privacy_gate._read_path_no_follow

            def fail_selected(path: Path, expected_size: int) -> bytes:
                if path.name == target.name:
                    raise PermissionError("synthetic")
                return original(path, expected_size)

            with mock.patch.object(privacy_gate, "_read_path_no_follow", side_effect=fail_selected):
                report = privacy_gate.scan_tree(root)
            self.assertIn("UNREADABLE_FILE", {item.rule_id for item in report.findings})


class DenylistTests(unittest.TestCase):
    def test_denylist_uses_nfkc_casefold_and_requires_private_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            term = "".join(("Project ", "Kappa"))
            denylist = base / "private-markers.txt"
            _write(denylist, term + "\n")
            denylist.chmod(0o600)
            terms = privacy_gate.load_private_denylist(root, denylist)
            _write(root / "notes.txt", "PROJECT KAPPA\n")
            report = privacy_gate.scan_tree(root, terms)
            self.assertIn("PRIVATE_DENYLIST", {item.rule_id for item in report.findings})

            inside = root / "private-markers.txt"
            _write(inside, "synthetic-marker\n")
            inside.chmod(0o600)
            with self.assertRaises(privacy_gate.GateConfigurationError) as caught:
                privacy_gate.load_private_denylist(root, inside)
            self.assertEqual(caught.exception.code, "DENYLIST_MUST_BE_OUTSIDE_REPOSITORY")

    @unittest.skipUnless(os.name == "posix", "POSIX permission mode is required")
    def test_denylist_requires_exact_0600_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            denylist = base / "private-markers.txt"
            _write(denylist, "synthetic-marker\n")
            denylist.chmod(0o644)
            with self.assertRaises(privacy_gate.GateConfigurationError) as caught:
                privacy_gate.load_private_denylist(root, denylist)
            self.assertEqual(caught.exception.code, "DENYLIST_MODE_MUST_BE_0600")


@unittest.skipUnless(shutil.which("git"), "Git is required for Git-object modes")
class GitScanTests(unittest.TestCase):
    def test_staged_scans_index_blob_not_worktree_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git(root)
            leaked_email = "".join(("staged-person", "@private", ".test"))
            target = root / "notes.txt"
            _write(target, leaked_email + "\n")
            _git(root, "add", "notes.txt")
            _write(target, "clean working-tree replacement\n")

            report = privacy_gate.scan_staged(root)
            self.assertIn("EMAIL", {item.rule_id for item in report.findings})
            rendered = "\n".join(privacy_gate.render_finding(item) for item in report.findings)
            self.assertNotIn(leaked_email, rendered)

    def test_sensitive_staged_and_history_paths_are_redacted(self) -> None:
        sensitive = "".join(("git-person", "@private", ".test"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git(root)
            _write(root / sensitive / "notes.txt", "synthetic\n")
            _git(root, "add", sensitive)

            staged = privacy_gate.scan_staged(root)
            self.assertIn("EMAIL", {item.rule_id for item in staged.findings})
            self.assertNotIn(sensitive, _render_report(staged))

            _git(root, "commit", "-q", "-m", "synthetic path fixture")
            history = privacy_gate.scan_history(root)
            self.assertIn("EMAIL", {item.rule_id for item in history.findings})
            self.assertNotIn(sensitive, _render_report(history))

    def test_history_scans_deleted_blobs_commit_and_annotated_tag_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git(root)
            leaked_email = "".join(("history-person", "@private", ".test"))
            private_marker = "".join(("private-", "release-", "marker"))
            _write(root / "old.txt", leaked_email + "\n")
            runtime_dir = root / ("run" + "time")
            _write(runtime_dir / "state.txt", "synthetic runtime state\n")
            _git(root, "add", "old.txt", runtime_dir.name)
            _git(root, "commit", "-q", "-m", "synthetic initial")
            (root / "old.txt").unlink()
            (runtime_dir / "state.txt").unlink()
            runtime_dir.rmdir()
            _git(root, "add", "-u")
            _git(root, "commit", "-q", "-m", "remove synthetic fixture")
            _git(root, "tag", "-a", "v0.0.0", "-m", private_marker)

            report = privacy_gate.scan_history(root, (privacy_gate._normal(private_marker),))
            rules = {item.rule_id for item in report.findings}
            self.assertIn("EMAIL", rules)
            self.assertIn("PRIVATE_DENYLIST", rules)
            self.assertIn("FORBIDDEN_RUNTIME_PATH", rules)
            self.assertTrue(any(item.path.startswith("history/tags/") for item in report.findings))
            rendered = "\n".join(privacy_gate.render_finding(item) for item in report.findings)
            self.assertNotIn(leaked_email, rendered)
            self.assertNotIn(private_marker, rendered)

    def test_history_without_commits_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git(root)
            report = privacy_gate.scan_history(root)
            self.assertTrue(report.ok, report.findings)
            self.assertEqual(report.scanned_files, 0)

    def test_history_scans_nested_reachable_tag_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git(root)
            _write(root / "README.md", "synthetic\n")
            _git(root, "add", "README.md")
            _git(root, "commit", "-q", "-m", "synthetic initial")
            nested_marker = "".join(("nested-", "private-", "marker"))
            _git(root, "tag", "-a", "inner-tag", "-m", nested_marker)
            _git(root, "tag", "-a", "outer-tag", "inner-tag", "-m", "synthetic outer")
            _git(root, "tag", "-d", "inner-tag")

            report = privacy_gate.scan_history(root, (privacy_gate._normal(nested_marker),))
            self.assertIn("PRIVATE_DENYLIST", {item.rule_id for item in report.findings})


class ReleaseArchiveTests(unittest.TestCase):
    def test_clean_tar_archive_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                _add_tar_bytes(bundle, "package/README.md", b"synthetic release\n")
            report = privacy_gate.scan_release_archive(archive)
            self.assertTrue(report.ok, report.findings)

    def test_release_allows_pinned_image_below_package_prefix(self) -> None:
        approved = (PROJECT_ROOT / "docs/assets/hermes-codex-operator-hero.png").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                _add_tar_bytes(
                    bundle,
                    "package/docs/assets/hermes-codex-operator-hero.png",
                    approved,
                )
            report = privacy_gate.scan_release_archive(archive)
            self.assertTrue(report.ok, report.findings)

    def test_release_rejects_pinned_image_below_two_prefixes(self) -> None:
        approved = (PROJECT_ROOT / "docs/assets/hermes-codex-operator-hero.png").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                _add_tar_bytes(
                    bundle,
                    "outer/package/docs/assets/hermes-codex-operator-hero.png",
                    approved,
                )
            report = privacy_gate.scan_release_archive(archive)
            self.assertIn("BINARY_FILE", {item.rule_id for item in report.findings})

    def test_sensitive_tar_path_is_redacted(self) -> None:
        sensitive = "".join(("archive-person", "@private", ".test"))
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                _add_tar_bytes(bundle, f"package/{sensitive}/notes.txt", b"synthetic\n")
            report = privacy_gate.scan_release_archive(archive)
            self.assertIn("EMAIL", {item.rule_id for item in report.findings})
            self.assertNotIn(sensitive, _render_report(report))

    def test_clean_wheel_shaped_zip_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package-0.0.0-py3-none-any.whl"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("package/__init__.py", "VALUE = 1\n")
                bundle.writestr("package-0.0.0.dist-info/METADATA", "Name: package\n")
            report = privacy_gate.scan_release_archive(archive)
            self.assertTrue(report.ok, report.findings)

    def test_zip_rejects_binary_symlink_and_redacts_sensitive_path(self) -> None:
        sensitive = "".join(("wheel-person", "@private", ".test"))
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package.whl"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(f"package/{sensitive}/notes.txt", "synthetic\n")
                bundle.writestr("package/image.bin", b"abc\0def")
                link = zipfile.ZipInfo("package/link")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(link, "target")
            report = privacy_gate.scan_release_archive(archive)
            rules = {item.rule_id for item in report.findings}
            self.assertTrue({"EMAIL", "BINARY_FILE", "SYMLINK_FORBIDDEN"} <= rules)
            self.assertNotIn(sensitive, _render_report(report))

    def test_tar_archive_rejects_traversal_link_binary_and_runtime_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.tar"
            runtime_name = "package/state" + ".d" + "b"
            with tarfile.open(archive, "w") as bundle:
                _add_tar_bytes(bundle, "../escape.txt", b"synthetic\n")
                _add_tar_bytes(bundle, "package/image.bin", b"abc\0def")
                _add_tar_bytes(bundle, runtime_name, b"synthetic\n")
                link = tarfile.TarInfo("package/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../outside"
                bundle.addfile(link)
            report = privacy_gate.scan_release_archive(archive)
            rules = {item.rule_id for item in report.findings}
            self.assertTrue(
                {
                    "UNSAFE_ARCHIVE_PATH",
                    "SYMLINK_FORBIDDEN",
                    "BINARY_FILE",
                    "FORBIDDEN_RUNTIME_PATH",
                }
                <= rules
            )


class OutputContractTests(unittest.TestCase):
    def test_rendered_finding_has_only_safe_fields(self) -> None:
        raw_value = "".join(("not-for-", "terminal-output"))
        finding = privacy_gate._finding("SYNTHETIC", "docs/example.txt", 7, raw_value)
        rendered = privacy_gate.render_finding(finding)
        payload = json.loads(rendered)
        self.assertEqual(
            set(payload),
            {"rule_id", "path", "line", "fingerprint"},
        )
        self.assertNotIn(raw_value, rendered)

    def test_cli_failure_does_not_print_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_value = "".join(("cli-person", "@private", ".test"))
            _write(root / "notes.txt", raw_value + "\n")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = privacy_gate.main((os.fspath(root), "--mode", "tree"))
            self.assertEqual(exit_code, 1)
            self.assertNotIn(raw_value, stdout.getvalue())
            self.assertNotIn(raw_value, stderr.getvalue())

    def test_repository_tree_is_clean(self) -> None:
        report = privacy_gate.scan_tree(PROJECT_ROOT)
        self.assertTrue(report.ok, report.findings)


if __name__ == "__main__":
    unittest.main()
