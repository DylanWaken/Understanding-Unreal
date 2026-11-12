#!/usr/bin/env python3
"""Focused tests for the portable frame-evidence verifier.

These tests cover the safety boundaries that are easy to regress: repository
path traversal, duplicate manifest paths, size/SHA mismatch, operand discovery
from nested replay evidence and explicit failure for a missing operand.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import verify_frame_evidence as verifier


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="lumen-verify-test-")
        self.root = Path(self._temporary.name).resolve()
        (self.root / "nested").mkdir()
        (self.root / "a.txt").write_bytes(b"alpha")
        (self.root / "nested/b.bin").write_bytes(b"beta")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write_manifest(self, files: list[dict]) -> Path:
        path = self.root / "artifact-manifest.json"
        path.write_text(json.dumps({"files": files}), encoding="utf-8")
        return path

    def test_exact_hashes_are_verified(self) -> None:
        manifest = self.write_manifest(
            [
                {"path": "a.txt", "bytes": 5, "sha256": digest(b"alpha")},
                {"path": "nested\\b.bin", "bytes": 4, "sha256": digest(b"beta")},
            ]
        )
        result = verifier.verify_artifacts(manifest, self.root)
        self.assertEqual(result, {"files": 2, "bytes": 9})

    def test_path_traversal_is_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_bytes(b"outside")
        try:
            manifest = self.write_manifest(
                [{"path": f"../{outside.name}", "bytes": 7, "sha256": digest(b"outside")}]
            )
            with self.assertRaisesRegex(verifier.EvidenceError, "traversal"):
                verifier.verify_artifacts(manifest, self.root)
        finally:
            outside.unlink(missing_ok=True)

    def test_duplicate_normalized_paths_are_rejected(self) -> None:
        manifest = self.write_manifest(
            [
                {"path": "a.txt", "bytes": 5, "sha256": digest(b"alpha")},
                {"path": "./A.TXT", "bytes": 5, "sha256": digest(b"alpha")},
            ]
        )
        with self.assertRaisesRegex(verifier.EvidenceError, "Duplicate artifact path"):
            verifier.verify_artifacts(manifest, self.root)

    def test_bad_hash_and_bad_size_are_rejected(self) -> None:
        bad_hash = self.write_manifest(
            [{"path": "a.txt", "bytes": 5, "sha256": "0" * 64}]
        )
        with self.assertRaisesRegex(verifier.EvidenceError, "SHA-256 mismatch"):
            verifier.verify_artifacts(bad_hash, self.root)
        bad_size = self.write_manifest(
            [{"path": "a.txt", "bytes": 4, "sha256": digest(b"alpha")}]
        )
        with self.assertRaisesRegex(verifier.EvidenceError, "size mismatch"):
            verifier.verify_artifacts(bad_size, self.root)


class ReplayEvidenceTests(unittest.TestCase):
    def test_nested_backslash_paths_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lumen-replay-test-") as temporary:
            replay = Path(temporary)
            evidence = {
                "outer": [
                    "Resources\\0001.mip0.v0002.bin",
                    {"inner": "C:\\capture\\Structures\\0003.bin"},
                    "not a payload.txt",
                ]
            }
            (replay / "replay.json").write_text(json.dumps(evidence), encoding="utf-8")
            spec = verifier.ReplaySpec("synthetic", "synthetic", "verify.py", 1, ("replay.json",))
            discovered = verifier.discover_payload_paths(replay, spec)
            self.assertEqual(
                discovered,
                {
                    "Resources/0001.mip0.v0002.bin",
                    "Structures/0003.bin",
                },
            )

    def test_basename_operand_resolves_only_to_one_capture_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lumen-replay-test-") as temporary:
            replay = Path(temporary)
            (replay / "replay.json").write_text(
                json.dumps({"nested": {"export": "0001.bin"}}), encoding="utf-8"
            )
            spec = verifier.ReplaySpec("synthetic", "synthetic", "verify.py", 1, ("replay.json",))
            discovered = verifier.discover_payload_paths(
                replay, spec, {"Resources/0001.bin", "Structures/0002.bin"}
            )
            self.assertEqual(discovered, {"Resources/0001.bin"})

    def test_ambiguous_basename_is_rejected_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lumen-replay-test-") as temporary:
            replay = Path(temporary)
            (replay / "replay.json").write_text(
                json.dumps({"export": "0001.bin"}), encoding="utf-8"
            )
            spec = verifier.ReplaySpec("synthetic", "synthetic", "verify.py", 1, ("replay.json",))
            with self.assertRaisesRegex(verifier.EvidenceError, "ambiguous"):
                verifier.discover_payload_paths(
                    replay,
                    spec,
                    {"Resources/0001.bin", "Structures/0001.bin"},
                )

    def test_missing_operand_is_named_not_skipped(self) -> None:
        spec = verifier.ReplaySpec("synthetic", "synthetic", "verify.py", 1, ("replay.json",))
        with self.assertRaisesRegex(verifier.EvidenceError, "Structures/missing.bin"):
            verifier.require_known_payloads(
                spec,
                {"Resources/known.bin", "Structures/missing.bin"},
                {"Resources/known.bin"},
            )


class RegisteredReplayTests(unittest.TestCase):
    def test_deferred_spec_pins_report_and_optional_skips(self) -> None:
        spec = next(item for item in verifier.REPLAY_REGISTRY if item.name == "deferred")
        self.assertEqual(spec.expected_checks, 17)
        self.assertEqual(spec.mode, "verify")
        self.assertIn("--report", spec.extra_args)
        self.assertIn("{replay_dir}/report.json", spec.extra_args)
        self.assertNotIn("--ltc-header", spec.extra_args)
        # The frozen scene-graph artifact supplies the real primary-view check.
        self.assertIn("--final-dir", spec.extra_args)
        self.assertIn("{final_dir}", spec.extra_args)
        self.assertEqual(
            set(spec.expected_skips),
            {"ltc_matches_ltc_h"},
        )

    def test_skipped_checks_are_separated_from_direct_proof(self) -> None:
        checks = [
            {"name": "direct", "passed": True, "evidence": {"value": 1}},
            {
                "name": "optional_source",
                "passed": True,
                "evidence": "skipped: --ltc-header not supplied",
            },
        ]
        direct, skipped = verifier._split_checks(checks)
        self.assertEqual(direct, 1)
        self.assertEqual(skipped, ["optional_source"])
        with tempfile.TemporaryDirectory(prefix="lumen-replay-test-") as temporary:
            path = Path(temporary) / "verification.json"
            path.write_text(
                json.dumps({"checks": checks, "passed": True}), encoding="utf-8"
            )
            spec = verifier.ReplaySpec(
                "synthetic", "synthetic", "verify.py", 2, ("replay.json",)
            )
            read_checks, all_ok = verifier._read_verification(path, spec)
            self.assertTrue(all_ok)
            self.assertEqual(verifier._split_checks(read_checks), (1, ["optional_source"]))


class GdfBoundaryTests(unittest.TestCase):
    def test_capture_root_normalization_is_the_only_byte_difference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lumen-gdf-test-") as temporary:
            root = Path(temporary)
            frozen_root = "D:\\public\\binary-20261112-153914"
            frozen_document = {
                "schema": "synthetic-gdf",
                "capture": {"root": frozen_root, "graph": "0001"},
                "evidenceHashes": {"capture": frozen_root},
                "value": 1,
            }
            fresh_document = json.loads(json.dumps(frozen_document))
            fresh_document["capture"]["root"] = "C:\\temp\\lumen-gdf-fresh"
            fresh_document["evidenceHashes"]["capture"] = "C:\\temp\\lumen-gdf-fresh"
            frozen = root / "frozen.json"
            fresh = root / "fresh.json"
            frozen.write_text(json.dumps(frozen_document, indent=1), encoding="utf-8")
            fresh.write_text(json.dumps(fresh_document, indent=1), encoding="utf-8")
            result = verifier.compare_gdf_replay(fresh, frozen)
            self.assertTrue(result["byteIdentical"])
            self.assertTrue(result["captureRootNormalized"])
            self.assertEqual(
                result["byteIdenticalSha256"],
                digest(frozen.read_bytes()),
            )

    def test_other_byte_difference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lumen-gdf-test-") as temporary:
            root = Path(temporary)
            frozen_root = "D:\\public\\binary-20261112-153914"
            frozen_document = {
                "capture": {"root": frozen_root},
                "evidenceHashes": {"capture": frozen_root},
                "value": 1,
            }
            fresh_document = json.loads(json.dumps(frozen_document))
            fresh_document["capture"]["root"] = "C:\\temp\\lumen-gdf-fresh"
            fresh_document["evidenceHashes"]["capture"] = "C:\\temp\\lumen-gdf-fresh"
            fresh_document["value"] = 2
            frozen = root / "frozen.json"
            fresh = root / "fresh.json"
            frozen.write_text(json.dumps(frozen_document, indent=1), encoding="utf-8")
            fresh.write_text(json.dumps(fresh_document, indent=1), encoding="utf-8")
            with self.assertRaisesRegex(verifier.EvidenceError, "byte-identical"):
                verifier.compare_gdf_replay(fresh, frozen)


if __name__ == "__main__":
    unittest.main()
