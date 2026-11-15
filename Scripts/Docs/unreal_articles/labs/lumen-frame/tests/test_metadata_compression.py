#!/usr/bin/env python3
"""Tests for the optional gzip metadata container.

Run from the repository root:
    python -m unittest tests.test_metadata_compression -v
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ROOT / "tools"
TOOL = TOOLS / "package_frame.py"
METADATA_TOOL = TOOLS / "package_metadata.py"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS))
from synthetic_fixture import build_fixture  # noqa: E402
from package_metadata import (  # noqa: E402
    MetadataError,
    convert_package,
    gzip_bytes,
    read_package_bytes,
    read_package_json,
)

GZIP_TOP_LEVEL = (
    "capture.json", "coverage.json", "inventory.json", "native-inputs.json",
    "passes.json", "resources.json", "schema.json", "structures.json",
)


def run_tool(*args: str, expect: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(ROOT), text=True, capture_output=True,
    )
    if result.returncode != expect:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expect}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class MetadataCompressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="frame-metadata-test-"))
        cls.dump = cls.tmp / "dump"
        build_fixture(cls.dump)
        cls.schema = cls.tmp / "source-schema.json"
        cls.schema.write_text(json.dumps({
            "schemaVersion": 1,
            "resourceSchemas": [],
            "parameterSchemas": [],
            "transforms": [],
            "mathTerms": [],
            "sourceRefs": [],
        }, indent=2) + "\n", encoding="utf-8")
        cls.raw = cls.tmp / "raw-package"
        cls.gzip = cls.tmp / "gzip-package"
        cls.raw_build = run_tool(
            "build", "--input", str(cls.dump), "--output", str(cls.raw),
            "--schema", str(cls.schema))
        cls.gzip_build = run_tool(
            "build", "--input", str(cls.dump), "--output", str(cls.gzip),
            "--schema", str(cls.schema),
            "--metadata-gzip", "--metadata-gzip-level", "6",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy_gzip_package(self, name: str) -> Path:
        dest = self.tmp / name
        shutil.copytree(self.gzip, dest)
        return dest

    def test_raw_layout_remains_default_and_compatible(self) -> None:
        self.assertIn("metadata compression: none (raw)", self.raw_build.stdout)
        self.assertTrue((self.raw / "index.json").is_file())
        self.assertTrue((self.raw / "inventory.json").is_file())
        self.assertFalse((self.raw / "inventory.json.gz").exists())
        index = json.loads((self.raw / "index.json").read_text(encoding="utf-8"))
        self.assertNotIn("metadata", index)
        self.assertEqual(index["schema"]["path"], "schema.json")
        self.assertEqual(index["files"]["resources"], "resources.json")
        self.assertEqual(index["files"]["index"], "index.json")
        resources = read_package_json(self.raw, "resources.json")
        self.assertTrue(all(item["record"].endswith(".json") for item in resources["items"]))
        run_tool("verify", "--package", str(self.raw))

    def test_gzip_layout_metadata_block_and_actual_paths(self) -> None:
        self.assertIn("metadata compression: gzip level 6", self.gzip_build.stdout)
        self.assertIn("metadata raw bytes:", self.gzip_build.stdout)
        self.assertIn("metadata stored bytes:", self.gzip_build.stdout)
        # index.json is the raw bootstrap; every other generated top-level JSON
        # and every record is stored as .json.gz with no raw twin.
        self.assertTrue((self.gzip / "index.json").is_file())
        self.assertFalse((self.gzip / "index.json.gz").exists())
        for name in GZIP_TOP_LEVEL:
            self.assertTrue((self.gzip / name).with_name(name + ".gz").is_file(), name)
            self.assertFalse((self.gzip / name).exists(), name)
        raw_records = list((self.raw / "records").rglob("*.json"))
        self.assertTrue(raw_records)
        for raw_record in raw_records:
            relative = raw_record.relative_to(self.raw)
            compressed = self.gzip / relative.parent / (relative.name + ".gz")
            self.assertTrue(compressed.is_file(), str(relative))
            self.assertFalse((self.gzip / relative).exists(), str(relative))
        index = json.loads((self.gzip / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["metadata"], {
            "compression": "gzip", "level": 6,
            "bootstrap": "index.json", "nativeCompression": "none",
        })
        for key in ("resources", "passes", "coverage", "schema", "structures",
                    "capture", "inventory", "nativeInputs"):
            value = index["files"][key]
            self.assertTrue(value.endswith(".json.gz"), (key, value))
            self.assertTrue((self.gzip / value).is_file(), value)
        self.assertEqual(index["files"]["index"], "index.json")
        self.assertEqual(index["schema"]["path"], "schema.json.gz")
        self.assertEqual(index["files"]["schema"], index["schema"]["path"])
        for doc_name in ("resources.json", "passes.json"):
            doc = read_package_json(self.gzip, doc_name)
            for item in doc["items"]:
                self.assertTrue(item["record"].endswith(".json.gz"), item["record"])
                self.assertTrue((self.gzip / item["record"]).is_file(), item["record"])

    def test_schema_native_and_chunks_are_byte_exact(self) -> None:
        source_schema = (self.raw / "schema.json").read_bytes()
        self.assertEqual(source_schema, self.schema.read_bytes())
        packaged_schema = read_package_bytes(self.gzip, "schema.json")
        self.assertEqual(packaged_schema, source_schema)
        index = json.loads((self.gzip / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"]["bytes"], len(source_schema))
        self.assertEqual(index["schema"]["sha256"], hashlib.sha256(source_schema).hexdigest())
        # Native copies stay raw and byte-identical; chunks are untouched.
        self.assertEqual(tree_hashes(self.raw / "native"), tree_hashes(self.gzip / "native"))
        self.assertFalse(any(path.name.endswith(".gz")
                             for path in (self.gzip / "native").rglob("*") if path.is_file()))
        self.assertEqual(tree_hashes(self.raw / "chunks"), tree_hashes(self.gzip / "chunks"))
        # Lazy records round-trip to the same logical JSON as the raw package.
        for raw_record in sorted((self.raw / "records").rglob("*.json")):
            relative = raw_record.relative_to(self.raw).as_posix()
            self.assertEqual(read_package_json(self.raw, relative),
                             read_package_json(self.gzip, relative), relative)

    def test_gzip_is_deterministic_with_zero_mtime(self) -> None:
        payload = b'{"deterministic": true}\n'
        first = gzip_bytes(payload, 6)
        self.assertEqual(first, gzip_bytes(payload, 6))
        self.assertEqual(gzip.decompress(first), payload)
        self.assertEqual(first[4:8], b"\x00\x00\x00\x00")
        build = (self.gzip / "inventory.json.gz").read_bytes()
        self.assertEqual(build[4:8], b"\x00\x00\x00\x00")

    def test_compressed_package_verifies_and_reconstructs(self) -> None:
        result = run_tool("verify", "--package", str(self.gzip))
        self.assertIn("Verified", result.stdout)
        recon = self.tmp / "gzip-recon"
        run_tool("reconstruct", "--package", str(self.gzip), "--dest", str(recon))
        for path in sorted(self.dump.rglob("*.bin")):
            relative = path.relative_to(self.dump)
            self.assertTrue((recon / relative).is_file(), str(relative))
            self.assertEqual(sha256_file(recon / relative), sha256_file(path), str(relative))

    def test_corrupt_gzip_and_missing_metadata_fail_verify(self) -> None:
        corrupt_record = self.copy_gzip_package("corrupt-record")
        record = corrupt_record / "records" / "resource" / "resource-0001.json.gz"
        data = record.read_bytes()
        record.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
        result = run_tool("verify", "--package", str(corrupt_record), expect=2)
        self.assertIn("error:", result.stderr.lower())
        self.assertIn("record metadata", result.stderr.lower())

        corrupt_schema = self.copy_gzip_package("corrupt-schema")
        schema = corrupt_schema / "schema.json.gz"
        data = schema.read_bytes()
        schema.write_bytes(data[:10] + b"\x00" + data[11:])
        result = run_tool("verify", "--package", str(corrupt_schema), expect=2)
        self.assertIn("error:", result.stderr.lower())

        missing = self.copy_gzip_package("missing-schema")
        (missing / "schema.json.gz").unlink()
        result = run_tool("verify", "--package", str(missing), expect=2)
        self.assertIn("error:", result.stderr.lower())

    def test_read_helpers_reject_unsafe_ambiguous_and_native_paths(self) -> None:
        with self.assertRaises(MetadataError):
            read_package_bytes(self.gzip, "../index.json")
        with self.assertRaises(MetadataError):
            read_package_bytes(self.gzip, "native/Base/Infos.json")
        with self.assertRaises(MetadataError):
            read_package_json(self.gzip, "chunks/" + "a" * 64 + ".gz")
        with self.assertRaises(MetadataError):
            read_package_json(self.gzip, "inventory.json.gz.extra.json")
        with self.assertRaises(MetadataError):
            read_package_json(self.gzip, "missing.json")
        ambiguous = self.copy_gzip_package("ambiguous")
        (ambiguous / "resources.json").write_bytes(b'{"raw": true}\n')
        with self.assertRaises(MetadataError):
            read_package_json(ambiguous, "resources.json")

    def test_conversion_copies_then_compresses_and_preserves_source(self) -> None:
        before = tree_hashes(self.raw)
        converted = self.tmp / "converted"
        result = subprocess.run(
            [sys.executable, str(METADATA_TOOL), "convert",
             "--input", str(self.raw), "--output", str(converted)],
            cwd=str(ROOT), text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree_hashes(self.raw), before, "conversion mutated the source package")
        index = json.loads((converted / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["metadata"]["compression"], "gzip")
        self.assertTrue(index["files"]["inventory"].endswith(".json.gz"))
        self.assertTrue((converted / "inventory.json.gz").is_file())
        self.assertFalse((converted / "inventory.json").exists())
        resources = read_package_json(converted, "resources.json")
        self.assertTrue(all(item["record"].endswith(".json.gz") for item in resources["items"]))
        run_tool("verify", "--package", str(converted))
        self.assertEqual(tree_hashes(self.raw / "native"), tree_hashes(converted / "native"))
        self.assertEqual(tree_hashes(self.raw / "chunks"), tree_hashes(converted / "chunks"))

    def test_conversion_rejects_same_nested_and_non_empty_outputs(self) -> None:
        with self.assertRaises(MetadataError):
            convert_package(self.raw, self.raw)
        with self.assertRaises(MetadataError):
            convert_package(self.raw, self.raw / "nested")
        with self.assertRaises(MetadataError):
            convert_package(self.raw, self.tmp)
        non_empty = self.tmp / "non-empty"
        non_empty.mkdir()
        (non_empty / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(MetadataError):
            convert_package(self.raw, non_empty)
        self.assertEqual((non_empty / "keep.txt").read_text(encoding="utf-8"), "keep")
        convert_package(self.raw, non_empty, force=True)
        self.assertTrue((non_empty / "index.json").is_file())
        self.assertFalse((non_empty / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
