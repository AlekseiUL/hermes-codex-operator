from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.docs_gate import scan_docs


class DocsGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_valid_links_anchor_mermaid_and_command_pass(self) -> None:
        self.write("docs/guide.md", "# Install me\n\nText.\n")
        self.write(
            "README.md",
            "# Product\n\n"
            "[Guide](docs/guide.md#install-me)\n\n"
            "```mermaid\nflowchart LR\n  A --> B\n```\n\n"
            "```bash\nhermes-codex submit --help\n```\n",
        )
        self.assertEqual(scan_docs(self.root, {"submit"}), [])

    def test_missing_target_and_anchor_fail(self) -> None:
        self.write("docs/guide.md", "# Existing\n")
        self.write(
            "README.md",
            "[Missing](docs/nope.md)\n[Anchor](docs/guide.md#absent)\n",
        )
        findings = scan_docs(self.root, {"submit"})
        self.assertTrue(any("missing link target" in item for item in findings))
        self.assertTrue(any("missing anchor" in item for item in findings))

    def test_unclosed_fence_fails(self) -> None:
        self.write("README.md", "# Product\n\n```bash\nhermes-codex submit\n")
        findings = scan_docs(self.root, {"submit"})
        self.assertTrue(any("unclosed" in item for item in findings))

    def test_empty_mermaid_and_unknown_command_fail(self) -> None:
        self.write(
            "README.md",
            "# Product\n\n```mermaid\n```\n\n"
            "```bash\nhermes-codex imaginary --json\n```\n",
        )
        findings = scan_docs(self.root, {"submit"})
        self.assertTrue(any("empty Mermaid" in item for item in findings))
        self.assertTrue(any("unknown hermes-codex command" in item for item in findings))

    def test_link_cannot_escape_repository(self) -> None:
        self.write("README.md", "[Outside](../outside.md)\n")
        findings = scan_docs(self.root, {"submit"})
        self.assertTrue(any("escapes repository" in item for item in findings))

    def test_generated_directories_are_ignored(self) -> None:
        self.write("README.md", "# Product\n")
        self.write("dist/README.md", "[Stale](missing.md)\n")
        self.write("node_modules/package/README.md", "```mermaid\n```\n")
        self.assertEqual(scan_docs(self.root, {"submit"}), [])


if __name__ == "__main__":
    unittest.main()
