from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDIST_ROOT = "package-0.0.0"
SDIST_NAME = f"{SDIST_ROOT}.tar.gz"


def _load_script(name: str):
    path = PROJECT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


normalize_sdist = _load_script("normalize_sdist")
privacy_gate = _load_script("privacy_gate")


def _entry(
    name: str,
    content: bytes | None,
    *,
    uid: int,
    owner: str,
    mtime: int,
    mode: int,
) -> tuple[tarfile.TarInfo, bytes | None]:
    member = tarfile.TarInfo(name)
    member.uid = uid
    member.gid = 20
    member.uname = owner
    member.gname = "local-group"
    member.mtime = mtime
    member.mode = mode
    if content is None:
        member.type = tarfile.DIRTYPE
    else:
        member.size = len(content)
    return member, content


def _raw_sdist(
    path: Path,
    *,
    unsafe: bool = False,
    reverse: bool = False,
    include_subdir: bool = True,
    uid: int = 501,
    owner: str = "local-builder",
    mtime: int = 1_700_000_000,
    file_mode: int = 0o664,
    extra_paths: tuple[str, ...] = (),
) -> None:
    entries = [
        _entry(
            SDIST_ROOT,
            None,
            uid=uid,
            owner=owner,
            mtime=mtime,
            mode=0o775,
        ),
        _entry(
            f"{SDIST_ROOT}/PKG-INFO",
            b"Metadata-Version: 2.4\nName: package\nVersion: 0.0.0\n",
            uid=uid,
            owner=owner,
            mtime=mtime,
            mode=file_mode,
        ),
        _entry(
            f"{SDIST_ROOT}/pyproject.toml",
            b"[build-system]\nrequires = []\n",
            uid=uid,
            owner=owner,
            mtime=mtime,
            mode=file_mode,
        ),
    ]
    if include_subdir:
        entries.append(
            _entry(
                f"{SDIST_ROOT}/sub",
                None,
                uid=uid,
                owner=owner,
                mtime=mtime,
                mode=0o775,
            )
        )
    implementation_name = (
        "../escape.py" if unsafe else f"{SDIST_ROOT}/sub/package.py"
    )
    entries.append(
        _entry(
            implementation_name,
            b"VALUE = 1\n",
            uid=uid,
            owner=owner,
            mtime=mtime,
            mode=file_mode,
        )
    )
    for name in extra_paths:
        entries.append(
            _entry(
                f"{SDIST_ROOT}/{name}",
                b"synthetic\n",
                uid=uid,
                owner=owner,
                mtime=mtime,
                mode=file_mode,
            )
        )
    if reverse:
        entries.reverse()

    with tarfile.open(path, "w:gz") as bundle:
        for member, content in entries:
            bundle.addfile(member, None if content is None else io.BytesIO(content))


class NormalizeSdistTests(unittest.TestCase):
    def test_normalizes_owner_time_mode_header_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / SDIST_NAME
            _raw_sdist(archive)

            result = normalize_sdist.normalize_sdist(
                archive,
                expected_root=SDIST_ROOT,
            )

            artifact = archive.read_bytes()
            self.assertEqual(result.output_sha256, hashlib.sha256(artifact).hexdigest())
            self.assertNotEqual(result.input_sha256, result.output_sha256)
            self.assertEqual(artifact[4:8], (315_532_800).to_bytes(4, "little"))
            self.assertEqual(artifact[3] & (0x08 | 0x10), 0)  # no FNAME/FCOMMENT
            with tarfile.open(archive, "r:gz") as bundle:
                members = bundle.getmembers()
                self.assertEqual(len(members), 5)
                self.assertEqual(
                    [member.name for member in members],
                    sorted(member.name for member in members),
                )
                for member in members:
                    self.assertEqual((member.uid, member.gid), (0, 0))
                    self.assertEqual((member.uname, member.gname), ("root", "root"))
                    self.assertEqual(member.mtime, 315_532_800)
                    self.assertEqual(member.mode, 0o755 if member.isdir() else 0o644)
                implementation = bundle.getmember(f"{SDIST_ROOT}/sub/package.py")
                extracted = bundle.extractfile(implementation)
                assert extracted is not None
                self.assertEqual(extracted.read(), b"VALUE = 1\n")

            report = privacy_gate.scan_release_archive(
                archive,
                deny_terms=("local-builder", "local-group"),
            )
            self.assertTrue(report.ok, report.findings)

    def test_equivalent_content_produces_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_dir = Path(directory) / "first"
            second_dir = Path(directory) / "second"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / SDIST_NAME
            second = second_dir / SDIST_NAME
            _raw_sdist(first)
            _raw_sdist(
                second,
                reverse=True,
                include_subdir=False,
                uid=1001,
                owner="another-builder",
                mtime=1_800_000_000,
                file_mode=0o755,
            )

            normalize_sdist.normalize_sdist(first, expected_root=SDIST_ROOT)
            normalize_sdist.normalize_sdist(second, expected_root=SDIST_ROOT)

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_casefold_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / SDIST_NAME
            _raw_sdist(archive, extra_paths=("README.md", "readme.md"))

            with self.assertRaises(normalize_sdist.NormalizationError) as raised:
                normalize_sdist.normalize_sdist(archive, expected_root=SDIST_ROOT)

            self.assertEqual(raised.exception.code, "COLLIDING_ARCHIVE_PATH")

    def test_unsafe_input_fails_without_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / SDIST_NAME
            _raw_sdist(archive, unsafe=True)
            before = archive.read_bytes()

            with self.assertRaises(normalize_sdist.NormalizationError) as raised:
                normalize_sdist.normalize_sdist(archive, expected_root=SDIST_ROOT)

            self.assertEqual(raised.exception.code, "UNSAFE_ARCHIVE_PATH")
            self.assertEqual(archive.read_bytes(), before)

    def test_filename_and_required_files_are_bound_to_expected_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / SDIST_NAME
            _raw_sdist(archive)

            with self.assertRaises(normalize_sdist.NormalizationError) as raised:
                normalize_sdist.normalize_sdist(
                    archive,
                    expected_root="different-0.0.0",
                )

            self.assertEqual(raised.exception.code, "SDIST_FILENAME_MISMATCH")


if __name__ == "__main__":
    unittest.main()
