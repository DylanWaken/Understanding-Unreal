#!/usr/bin/env python3
"""Tests for the lossless frame packer and normalized import contract.

Run from the repository root:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOL = ROOT / "tools" / "package_frame.py"
FIXTURE_BUILDER = HERE / "synthetic_fixture.py"
sys.path.insert(0, str(HERE))
from synthetic_fixture import (  # noqa: E402
    CHUNK, add_depth_stencil_subresources, add_instrumented_sidecars,
    add_viewport_fixture, build_fixture,
)


def parse_json_sequence(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    index = 0
    items = []
    while index < len(text):
        while index < len(text) and text[index] in " \r\n\t":
            index += 1
        if index >= len(text):
            break
        item, index = decoder.raw_decode(text, index)
        items.append(item)
    return items


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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_schema(path: Path, unsafe_marker: Path | None = None) -> None:
    unsafe = "__import__('pathlib').Path(" + repr(str(unsafe_marker or Path("unsafe-marker"))) + ").write_text('EXECUTED')"
    schema = {
        "schemaVersion": 1,
        "resourceSchemas": [
            {
                "id": "synthetic.direct-texel",
                "selectors": [{"resourceNameRegex": r"Lumen\.Synthetic\.DirectLighting", "passNameRegex": r"Lumen\.Synthetic"}],
                "quantity": "synthetic irradiance",
                "units": "arbitrary",
                "coordinateSpace": "texel",
                "storage": {
                    "kind": "texel",
                    "format": "PF_FloatR11G11B10",
                    "strideBytes": 8,
                    "formatAlternatives": ["PF_FloatRGBA"],
                    "formatVariants": [
                        {
                            "format": "PF_FloatRGBA",
                            "strideBytes": 8,
                            "sourceRefs": ["synthetic.src"],
                            "components": [
                                {"name": "variantR", "type": "float16", "byteOffset": 0},
                                {"name": "variantG", "type": "float16", "byteOffset": 2},
                            ],
                        },
                        {
                            "format": "PF_A32B32G32R32F",
                            "strideBytes": 16,
                            "components": [{"name": "variantR", "type": "float32", "byteOffset": 0}],
                        },
                    ],
                },
                "fields": [
                    {"name": "topLeftRG", "type": "float16", "byteOffset": 0, "count": 2, "decode": "half",
                     "units": "arbitrary", "sourceRefs": ["synthetic.src"]},
                    {"name": "unsafeField", "type": "uint32", "byteOffset": 4, "decode": unsafe,
                     "sourceRefs": ["synthetic.src"]},
                    {"name": "equationField", "type": "uint32", "byteOffset": 8, "decode": "stored / 1023",
                     "sourceRefs": ["synthetic.src"]},
                    {"name": "gatedR11", "type": "float11", "bitOffset": 0, "bitWidth": 11,
                     "decode": "none", "appliesToFormats": ["PF_FloatR11G11B10"],
                     "sourceRefs": ["synthetic.src"]},
                ],
                "transforms": [{"id": "synthetic.tr", "equation": "display = clamp(value,0,1)", "sourceRefs": ["synthetic.src"]}],
                "mathTerms": [{"id": "synthetic.irr", "term": "E", "definition": "synthetic example", "sourceRefs": ["synthetic.src"]}],
                "producerSources": ["synthetic.src"],
                "consumerSources": [],
                "decodingStatus": "partial",
                "gaps": ["synthetic only"],
            }
        ],
        "parameterSchemas": [
            {
                "id": "synthetic.direct-pass",
                "selectors": [{"passNameRegex": "DirectLighting"}],
                "quantity": "synthetic parameter",
                "fields": [
                    {"name": "LightingMode", "type": "float32", "byteOffset": 0, "decode": "float", "sourceRefs": ["synthetic.src"]},
                    {"name": "UnsafeExpression", "type": "uint32", "byteOffset": 4, "decode": unsafe, "sourceRefs": ["synthetic.src"]},
                ],
                "decodingStatus": "partial",
            }
        ],
        "transforms": [],
        "mathTerms": [],
        "sourceRefs": [
            {"id": "synthetic.src", "path": "SyntheticShaders.usf", "line": 42, "symbol": "Synthetic", "claim": "Synthetic test anchor only."}
        ],
    }
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")


class PackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="frame-packer-test-"))
        self.dump = self.tmp / "dump"
        build_fixture(self.dump)
        self.package = self.tmp / "package"
        self.schema = self.tmp / "schema.json"
        write_schema(self.schema, self.tmp / "unsafe-marker")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *extra: str) -> subprocess.CompletedProcess:
        return run_tool("build", "--input", str(self.dump), "--output", str(self.package),
                        "--schema", str(self.schema), *extra)

    def test_manifest_counts_and_native_inventory(self) -> None:
        result = self.build()
        self.assertIn("PACKAGE COMPLETE", result.stdout)
        index = json.loads((self.package / "index.json").read_text(encoding="utf-8"))
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(index["counts"]["resourceRecords"], 6)
        self.assertEqual(index["counts"]["passRecords"], 3)
        self.assertEqual(index["counts"]["payloadFiles"], 10)
        self.assertEqual(index["counts"]["parameterBlocks"], 4)
        self.assertEqual(index["counts"]["nativeRecords"], 34)
        self.assertGreaterEqual(index["counts"]["nativeFiles"], 13)
        self.assertFalse(index["coverage"]["complete"])
        self.assertTrue(index["fixture"])
        # Every raw input .bin has an inventory record and a retained package path.
        payload_paths = {p["originalPath"] for p in inventory["payloads"]}
        for path in sorted(self.dump.rglob("*.bin")):
            rel = path.relative_to(self.dump).as_posix()
            self.assertIn(rel, payload_paths, rel)
            record = next(r for r in inventory["nativeRecords"] if r.get("kind") == "payload" and r.get("sourcePath") == rel)
            self.assertEqual(record["bytes"], path.stat().st_size)
            self.assertEqual(record["sha256"], sha256(path))
        # Every non-bin file is retained byte-for-byte.
        for path in sorted(self.dump.rglob("*")):
            if path.is_dir() or path.suffix.lower() == ".bin":
                continue
            rel = path.relative_to(self.dump).as_posix()
            copied = self.package / "native" / rel
            self.assertTrue(copied.exists(), rel)
            self.assertEqual(sha256(copied), sha256(path), rel)
        # Every parsed native record is represented in the inventory.
        expected_kinds = {
            "resource-descriptor": 6, "pass": 3, "pass-draw-count": 2,
            "referenced-structs": 1, "infos": 1, "status": 1,
            "console-variable": 3, "unknown-json": 2, "uniform-structure": 4,
            "payload": 10, "file": 1,
        }
        self.assertEqual(index["counts"]["nativeRecordKinds"], expected_kinds)

    def test_lossless_chunk_dedup_and_reconstruction(self) -> None:
        self.build()
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        index = json.loads((self.package / "index.json").read_text(encoding="utf-8"))
        chunks = inventory["chunks"]
        self.assertTrue(chunks)
        self.assertTrue(all(c["rawLength"] <= 1_048_576 for c in chunks))
        self.assertLess(index["counts"]["uniqueChunkBytes"], index["counts"]["payloadBytes"])
        # The two large probe buffers are byte-identical and must share chunks.
        by_name = {p["originalPath"]: p for p in inventory["payloads"]}
        big_paths = [p for p in by_name if "0001000000000000000c" in p or "0001000000000000000d" in p]
        self.assertEqual(len(big_paths), 2)
        big = [by_name[p] for p in big_paths]
        self.assertEqual(big[0]["sha256"], big[1]["sha256"])
        self.assertEqual([c["sha"] for c in big[0]["chunks"]], [c["sha"] for c in big[1]["chunks"]])
        # Verify mode reconstructs every payload in memory and checks hashes.
        result = run_tool("verify", "--package", str(self.package))
        self.assertIn("Verified", result.stdout)
        # Reconstruct to a separate directory and compare every .bin.
        recon = self.tmp / "reconstructed"
        run_tool("reconstruct", "--package", str(self.package), "--dest", str(recon))
        for path in sorted(self.dump.rglob("*.bin")):
            rel = path.relative_to(self.dump)
            self.assertTrue((recon / rel).exists(), str(rel))
            self.assertEqual(sha256(recon / rel), sha256(path), str(rel))

    def test_estimate_writes_nothing(self) -> None:
        estimate_dir = self.tmp / "estimate-output"
        result = run_tool("build", "--input", str(self.dump), "--output", str(estimate_dir),
                          "--estimate", "--schema", str(self.schema))
        self.assertIn("ESTIMATE", result.stdout)
        self.assertFalse(estimate_dir.exists())
        self.assertIn("unique chunk raw bytes", result.stdout)
        self.assertIn("compressed chunk bytes", result.stdout)
        self.assertIn("largest output", result.stdout)

    def test_schema_join_and_unsafe_decode_not_evaluated(self) -> None:
        # The schema contains a decode string that would create this marker if
        # anything ever evaluated it as Python.
        marker = self.tmp / "unsafe-marker"
        self.build()
        self.assertFalse(marker.exists(), "schema decode string was evaluated")
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        index = json.loads((self.package / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"]["status"], "joined")
        # The packaged schema is evidence, not a re-serialized rendering: it must
        # match the supplied bytes and the hash/size recorded in index.json.
        self.assertEqual((self.package / "schema.json").read_bytes(), self.schema.read_bytes())
        self.assertEqual(index["schema"]["path"], "schema.json")
        self.assertEqual((self.package / index["schema"]["path"]).read_bytes(), self.schema.read_bytes())
        self.assertEqual(index["schema"]["sha256"], hashlib.sha256(self.schema.read_bytes()).hexdigest())
        self.assertEqual(index["schema"]["bytes"], self.schema.stat().st_size)
        self.assertEqual(index["counts"]["schemaMatchedResources"], 1)
        self.assertEqual(index["counts"]["schemaMatchedPasses"], 1)
        gap_kinds = {g["kind"] for g in inventory["gaps"]}
        self.assertIn("schema-field-decode-unsafe-expression", gap_kinds)
        self.assertIn("schema-field-source-equation", gap_kinds)
        resource_record = json.loads((self.package / "records" / "resource" / "resource-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(resource_record["schemaMatches"][0]["schemaId"], "synthetic.direct-texel")
        support = resource_record["schemaMatches"][0]["fieldSupport"]
        self.assertIn("unsafeField", support["unsafeExpressionFields"])
        self.assertIn("equationField", support["sourceEquationFields"])
        self.assertNotIn("equationField", support["unsafeExpressionFields"])
        # The dead producer/consumer key is gone: warnings must read the real keys.
        for match in resource_record["schemaMatches"]:
            self.assertNotIn("unsupportedDecodeFields", match["fieldSupport"])
        resources_doc = json.loads((self.package / "resources.json").read_text(encoding="utf-8"))
        summary = next(r for r in resources_doc["items"] if r["name"] == "Lumen.Synthetic.DirectLighting")
        self.assertTrue(summary["hasUnsafeDecodeExpression"])
        self.assertTrue(summary["hasSourceEquationFields"])
        self.assertEqual(summary["sourceEquationFieldCount"], 1)
        # The raw payload is present and unchanged; no value was fabricated for the unsafe field.
        payload = resource_record["payloads"][0]
        self.assertEqual(payload["bytes"], 32)
        self.assertEqual(len(payload["chunks"]), 1)

    def test_format_variants_gate_wrong_format_fields(self) -> None:
        self.build()
        record = json.loads((self.package / "records" / "resource" / "resource-0001.json").read_text(encoding="utf-8"))
        match = record["schemaMatches"][0]
        # The captured resource is PF_FloatRGBA while the schema primary layout
        # is R11G11B10.  The matching formatVariants entry becomes active, and
        # every primary/ungated field is inactive rather than read at R11 offsets.
        self.assertEqual(match["declaredFormat"], "PF_FloatR11G11B10")
        self.assertEqual(match["payloadFormat"], "PF_FLOATRGBA")
        self.assertFalse(match["payloadFormatMatch"])
        self.assertTrue(match["extractionEligible"])
        self.assertEqual(match["activeFormatVariant"], "PF_FloatRGBA")
        self.assertEqual(
            sorted(match["formatGatedFields"]),
            ["equationField", "gatedR11", "topLeftRG", "unsafeField"],
        )
        self.assertEqual(
            [(field["name"], field["type"], field["byteOffset"], field["appliesToFormats"])
             for field in match["formatVariantFields"]],
            [("variantR", "float16", 0, ["PF_FloatRGBA"]),
             ("variantG", "float16", 2, ["PF_FloatRGBA"])],
        )
        # The full declared variant list is retained, including the A32 variant
        # that this capture did NOT use.
        variants = match["storage"]["formatVariants"]
        self.assertEqual([variant["format"] for variant in variants],
                         ["PF_FloatRGBA", "PF_A32B32G32R32F"])
        index = json.loads((self.package / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"]["formatVariants"], 2)
        self.assertEqual(index["schema"]["formatGatedFields"], 1)

    def test_unknown_format_and_missing_payload_are_explicit(self) -> None:
        self.build()
        resources = json.loads((self.package / "resources.json").read_text(encoding="utf-8"))["items"]
        by_name = {r["name"]: r for r in resources}
        unknown = by_name["Lumen.Synthetic.AmbiguousFormat"]
        self.assertFalse(unknown["decodeSupported"])
        self.assertGreaterEqual(unknown["gapCount"], 1)
        missing = by_name["Lumen.Synthetic.MissingPayload"]
        self.assertEqual(missing["payloadCount"], 0)
        coverage = json.loads((self.package / "coverage.json").read_text(encoding="utf-8"))
        kinds = {g["kind"] for g in coverage["gaps"]}
        self.assertIn("unsupported-pixel-format", kinds)
        self.assertIn("resource-payload-missing", kinds)
        self.assertEqual(len(coverage["unsupportedFormats"]), 1)
        self.assertEqual(coverage["unsupportedFormats"][0]["format"], "PF_R16G16B16A16_UINT")

    def test_corrupt_chunk_is_detected(self) -> None:
        self.build()
        chunks = list((self.package / "chunks").glob("*.gz"))
        self.assertTrue(chunks)
        original = chunks[0].read_bytes()
        chunks[0].write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
        result = run_tool("verify", "--package", str(self.package), expect=2)
        self.assertIn("error:", result.stderr.lower())

    def test_output_inside_input_is_refused(self) -> None:
        out = self.dump / "nested-package"
        result = run_tool("build", "--input", str(self.dump), "--output", str(out), expect=2)
        self.assertIn("must not be inside", result.stderr)
        self.assertFalse(out.exists())

    def test_normalized_path_traversal_is_refused(self) -> None:
        norm = self.tmp / "normalized"
        norm.mkdir()
        (norm / "payload.bin").write_bytes(b"safe")
        manifest = {
            "format": "unreal-frame-normalized-v1",
            "capture": {"fixture": True},
            "resources": [{"id": "res-1", "name": "R", "kind": "buffer", "byteSize": 4, "metadata": {"Desc": "FRDGBufferDesc"}}],
            "passes": [{"id": "pass-1", "name": "P", "inputs": [], "outputs": ["res-1"]}],
            "payloads": [{"path": "../escape.bin", "role": "resource", "resourceId": "res-1"}],
        }
        (norm / "normalized-frame.json").write_text(json.dumps(manifest), encoding="utf-8")
        out = self.tmp / "normalized-package"
        result = run_tool("build", "--input", str(norm), "--output", str(out), "--adapter", "normalized", expect=2)
        self.assertIn("traversal", result.stderr.lower())
        self.assertFalse((self.tmp / "escape.bin").exists())

    def test_normalized_adapter_counts_records_and_links_payloads(self) -> None:
        norm = self.tmp / "normalized-ok"
        norm.mkdir()
        data = b"\x01\x02\x03\x04"
        (norm / "payload.bin").write_bytes(data)
        (norm / "normalized-frame.json").write_text(json.dumps({
            "format": "unreal-frame-normalized-v1",
            "status": "ok",
            "fixture": True,
            "capture": {"Project": "NormalizedFixture", "fixture": True},
            "resources": [{"id": "res-1", "name": "R", "kind": "buffer", "byteSize": 4, "metadata": {"Desc": "FRDGBufferDesc", "Name": "R"}}],
            "passes": [{"id": "pass-1", "name": "P", "inputs": [], "outputs": ["res-1"], "metadata": {"EventName": "P"}}],
            "payloads": [{"path": "payload.bin", "role": "resource", "resourceId": "res-1", "passId": "pass-1", "sha256": sha256(norm / "payload.bin")}],
        }), encoding="utf-8")
        out = self.tmp / "normalized-ok-package"
        run_tool("build", "--input", str(norm), "--output", str(out), "--adapter", "normalized")
        inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(inventory["payloads"][0]["originalPath"], "payload.bin")
        self.assertEqual(inventory["payloads"][0]["sha256"], sha256(norm / "payload.bin"))
        self.assertEqual(inventory["counts"]["resources"], 1)
        self.assertEqual(inventory["counts"]["passes"], 1)

    def test_parameter_block_and_metadata_are_retained(self) -> None:
        self.build()
        record = json.loads((self.package / "records" / "pass" / "pass-0001.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(record["parameterBlock"])
        self.assertEqual(record["parameterBlock"]["bytes"], 24)
        self.assertEqual(record["parameterMetadata"]["StructTypeName"], "FLumenSyntheticDirectLightingCS")
        self.assertEqual(record["parameterMetadata"]["Members"][0]["Name"], "LightingMode")
        self.assertEqual(record["inputs"][0]["mip"], 0)
        self.assertEqual(record["outputs"][0]["mip"], 0)
        # The captured block is retained exactly and resolves the first float.
        recon = self.tmp / "parameter-recon"
        run_tool("reconstruct", "--package", str(self.package), "--dest", str(recon))
        source_block = next(self.dump.glob("Structures/00000000000000000101.bin")).read_bytes()
        captured_block = (recon / "Structures/00000000000000000101.bin").read_bytes()
        self.assertEqual(captured_block, source_block)
        self.assertEqual(int.from_bytes(captured_block[0:4], "little"), 0x40000000)  # 2.0f
        self.assertEqual(int.from_bytes(captured_block[4:8], "little"), 42)

    def test_native_uniform_payload_uses_final_ingested_bytes(self) -> None:
        add_instrumented_sidecars(self.dump)
        self.build("--force")
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        payloads = {p["id"]: p for p in inventory["payloads"]}
        checked = 0
        for path in sorted((self.package / "records" / "pass").glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for native in record.get("nativeUniformBuffers", []):
                embedded = native.get("uniformBufferPayload")
                if embedded is None:
                    continue
                checked += 1
                self.assertEqual(embedded, payloads[embedded["id"]], native.get("uniformBuffer"))
                self.assertGreater(embedded["bytes"], 0, native.get("uniformBuffer"))
                self.assertTrue(embedded["sha256"], native.get("uniformBuffer"))
                self.assertTrue(embedded["chunks"], native.get("uniformBuffer"))
        self.assertGreaterEqual(checked, 2)
        target = json.loads((self.package / "records" / "pass" / "pass-0004.json").read_text(encoding="utf-8"))
        uniform = target["nativeUniformBuffers"][0]
        embedded = uniform["uniformBufferPayload"]
        self.assertEqual(embedded["originalPath"], "Structures/00000000000000000106.bin")
        self.assertEqual(embedded["bytes"], 16)
        self.assertEqual(embedded["sha256"], sha256(self.dump / embedded["originalPath"]))
        self.assertEqual(len(embedded["chunks"]), 1)
        source = (self.dump / embedded["originalPath"]).read_bytes()
        self.assertEqual(source[:4], b"\x00\x00\x40\x40")
        self.assertEqual(source[-4:], b"\x00\x00\xc0\x40")

    def test_unbound_native_uniform_payload_stays_null(self) -> None:
        add_instrumented_sidecars(self.dump)
        (self.dump / "Structures" / "00000000000000000106.bin").unlink()
        self.build("--force")
        target = json.loads((self.package / "records" / "pass" / "pass-0004.json").read_text(encoding="utf-8"))
        uniform = target["nativeUniformBuffers"][0]
        self.assertEqual(uniform["uniformBuffer"], "00000000000000000106")
        self.assertIsNone(uniform["uniformBufferPayload"])
        for path in sorted((self.package / "records" / "pass").glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for native in record.get("nativeUniformBuffers", []):
                embedded = native.get("uniformBufferPayload")
                if embedded is not None:
                    self.assertGreater(embedded["bytes"], 0, native.get("uniformBuffer"))
                    self.assertTrue(embedded["sha256"], native.get("uniformBuffer"))

    def test_resource_typed_values_and_search_text(self) -> None:
        self.build()
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        resources = json.loads((self.package / "resources.json").read_text(encoding="utf-8"))["items"]
        passes = json.loads((self.package / "passes.json").read_text(encoding="utf-8"))["items"]
        target = next(r for r in resources if r["name"] == "Lumen.Synthetic.DirectLighting")
        self.assertIn("synthetic.direct-texel", target["searchText"])
        self.assertIn("directlighting", target["searchText"])
        pass_item = next(p for p in passes if p["name"] == "Lumen.Synthetic.DirectLighting")
        self.assertIn("lightingmode", pass_item["searchText"])
        self.assertIn("directlighting", pass_item["searchText"])
        # Every resource and pass summary points at an existing lazy record.
        for item in resources + passes:
            self.assertTrue((self.package / item["record"]).exists(), item["record"])
        # Reconstruct the 2x2 half-float texture and verify its native values.
        payload = next(p for p in inventory["payloads"] if p["originalPath"].startswith("Resources/0001000000000000000a"))
        recon = self.tmp / "typed-recon"
        run_tool("reconstruct", "--package", str(self.package), "--dest", str(recon))
        tex_bytes = (recon / payload["originalPath"]).read_bytes()
        import struct
        self.assertEqual(struct.unpack("<4e", tex_bytes[:8]), (1.0, 0.5, 2.0, 1.0))

    def test_cross_chunk_references_are_ordered(self) -> None:
        self.build()
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        large = next(p for p in inventory["payloads"] if p["originalPath"].startswith("Resources/0001000000000000000c"))
        self.assertEqual([c["length"] for c in large["chunks"]], [CHUNK, CHUNK, 124])
        self.assertEqual([c["offset"] for c in large["chunks"]], [0, CHUNK, 2 * CHUNK])

    def test_bad_indices_do_not_mutate_or_crash(self) -> None:
        self.build()
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        # All payload lengths and chunk offsets are internally consistent.
        for payload in inventory["payloads"]:
            offset = 0
            for ref in payload["chunks"]:
                self.assertEqual(ref["offset"], offset)
                self.assertGreater(ref["length"], 0)
                self.assertLessEqual(ref["length"], 1_048_576)
                offset += ref["length"]
            self.assertEqual(offset, payload["bytes"])


    def test_depth_and_stencil_bindings_are_disjoint_by_version(self) -> None:
        add_depth_stencil_subresources(self.dump)
        self.build()
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        payloads = {p["id"]: p for p in inventory["payloads"]}
        depth_payloads = [p for p in inventory["payloads"] if "00010000000000000010" in p["originalPath"]]
        self.assertEqual(len(depth_payloads), 4)
        stencil_ids = {p["id"] for p in depth_payloads if p["stencil"]}
        self.assertEqual(len(stencil_ids), 2)
        colour_ids = {p["id"] for p in depth_payloads if not p["stencil"]}
        self.assertEqual(len(colour_ids), 2)

        resource = json.loads((self.package / "records" / "resource" / "resource-0007.json").read_text(encoding="utf-8"))
        self.assertEqual({p["id"] for p in resource["payloads"]}, stencil_ids | colour_ids)
        self.assertEqual(len(resource["versions"]), 2)
        version_payloads = set()
        for version in resource["versions"]:
            self.assertEqual(len(version["payloadIds"]), 2)
            version_payloads.update(version["payloadIds"])
        self.assertEqual(version_payloads, stencil_ids | colour_ids)

        bindings = []
        for pass_id in ("pass-0004", "pass-0005"):
            record = json.loads((self.package / "records" / "pass" / f"{pass_id}.json").read_text(encoding="utf-8"))
            bindings.extend(record["inputs"] + record["outputs"])
        self.assertEqual(len(bindings), 6)
        for item in bindings:
            self.assertEqual(item["resourceId"], resource["id"])
            self.assertIsNotNone(item["version"])
            self.assertEqual(item["mip"], 0)
            self.assertIsNone(item["slice"])
            for payload_id in item["payloadIds"]:
                payload = payloads[payload_id]
                self.assertEqual(payload["version"], item["version"])
                self.assertEqual(payload["mip"], item["mip"])
                self.assertEqual(payload["slice"], item["slice"])
                self.assertEqual(bool(payload["stencil"]), bool(item["stencil"]),
                                 f"{item['uniqueName']} mixed stencil and colour payloads")
            if item["stencil"]:
                self.assertEqual(set(item["payloadIds"]), stencil_ids & {p["id"] for p in depth_payloads if p["version"] == item["version"]})
            else:
                self.assertEqual(set(item["payloadIds"]), colour_ids & {p["id"] for p in depth_payloads if p["version"] == item["version"]})

    def test_instrumented_3d_cube_and_array_layouts(self) -> None:
        add_instrumented_sidecars(self.dump)
        self.build("--force")
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        payloads = {p["id"]: p for p in inventory["payloads"]}
        resources = json.loads((self.package / "resources.json").read_text(encoding="utf-8"))["items"]
        by_name = {r["name"]: r for r in resources}
        for name in ("Lumen.Synthetic.Volume3D", "Lumen.Synthetic.CubeFaces", "Lumen.Synthetic.ArrayMips"):
            record = json.loads((self.package / by_name[name]["record"]).read_text(encoding="utf-8"))
            self.assertTrue(record["subresourceLayouts"], name)
            for item in record["subresourceLayouts"]:
                payload = payloads[item["payloadId"]]
                self.assertEqual(payload["originalPath"], item["file"])
                self.assertTrue(payload["completion"]["observed"], name)
                self.assertEqual(payload["completion"]["statusRecord"]["Status"], "Dumped")
                self.assertEqual(payload["completion"]["timing"], "GPUReadbackCompleted")
                layout = item["layout"]
                self.assertEqual(payload["mip"], layout["MipLevel"])
                self.assertEqual(payload["face"], layout.get("CubeFace"))
        volume = json.loads((self.package / by_name["Lumen.Synthetic.Volume3D"]["record"]).read_text(encoding="utf-8"))
        self.assertEqual(len(volume["subresourceLayouts"]), 1)
        volume_payload = payloads[volume["subresourceLayouts"][0]["payloadId"]]
        self.assertEqual(volume_payload["layout"]["Dimension"], "Texture3D")
        self.assertEqual(volume_payload["layout"]["ZSliceCount"], 2)
        self.assertEqual(volume_payload["layout"]["RowPitchBytes"], 8)
        self.assertEqual(volume_payload["layout"]["ZSliceStrideBytes"], 16)
        cube = json.loads((self.package / by_name["Lumen.Synthetic.CubeFaces"]["record"]).read_text(encoding="utf-8"))
        self.assertEqual(len(cube["subresourceLayouts"]), 6)
        self.assertEqual(sorted(item["layout"]["CubeFace"] for item in cube["subresourceLayouts"]), list(range(6)))
        self.assertTrue(all(item["layout"]["NumArraySlices"] == 1 for item in cube["subresourceLayouts"]))
        array = json.loads((self.package / by_name["Lumen.Synthetic.ArrayMips"]["record"]).read_text(encoding="utf-8"))
        self.assertEqual(len(array["subresourceLayouts"]), 4)
        self.assertEqual(sorted({item["layout"]["FirstArraySlice"] for item in array["subresourceLayouts"]}), [0, 1])
        self.assertEqual(sorted({item["layout"]["MipLevel"] for item in array["subresourceLayouts"]}), [0, 1])
        self.assertEqual(sorted({(item["layout"]["FirstArraySlice"], item["layout"]["MipLevel"],
                                 item["layout"]["ExtentX"], item["layout"]["ExtentY"])
                                for item in array["subresourceLayouts"]}),
                         [(0, 0, 2, 2), (0, 1, 1, 1), (1, 0, 2, 2), (1, 1, 1, 1)])

    def test_native_typed_subrange_completion_and_reuse(self) -> None:
        add_instrumented_sidecars(self.dump)
        self.build("--force")
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        native = next(p for p in inventory["payloads"] if p["originalPath"].startswith("Resources/NativeBuffer."))
        self.assertEqual(native["classification"], "native-buffer-srv-subrange")
        self.assertEqual(native["identity"]["viewOffsetInBytes"], 4)
        self.assertEqual(native["identity"]["viewSizeInBytes"], 8)
        self.assertEqual(native["identity"]["viewNumElements"], 2)
        self.assertEqual(native["identity"]["viewFormat"], "PF_R32_UINT")
        self.assertEqual(native["bytes"], 8)
        self.assertEqual(native["completion"]["statusRecord"]["Status"], "Dumped")
        # The producing pass exposes the typed range and all raw fields.
        inst = json.loads((self.package / "records" / "pass" / "pass-0004.json").read_text(encoding="utf-8"))
        binding = inst["nativeUniformBuffers"][0]["bindings"]["View.View.SyntheticTypedBuffer"]
        self.assertEqual(binding["ViewOffsetInBytes"], 4)
        self.assertEqual(binding["ViewStrideInBytes"], 4)
        self.assertEqual(binding["ViewFormat"], "PF_R32_UINT")
        self.assertEqual(binding["payloadIds"], [native["id"]])
        self.assertIsNone(binding["completion"].get("reason"))
        self.assertEqual(binding["completion"]["timing"], "GPUReadbackCompleted")
        self.assertFalse(inst["nativeUniformBuffers"][0]["ownerPassExecutionDependencyClaimed"])
        self.assertFalse(inst["nativeUniformBuffers"][0]["sourceStateProvenAtScheduling"])
        # A second pass reuses the same scheduled readback explicitly.
        p1 = json.loads((self.package / "records" / "pass" / "pass-0001.json").read_text(encoding="utf-8"))
        reuse = p1["nativeUniformBuffers"][0]["bindings"]["View.View.SyntheticVolume"]
        self.assertTrue(reuse["ReusedScheduledReadback"])
        self.assertEqual(reuse["FirstScheduledByPass"], "00010000000000000006")
        self.assertEqual(reuse["DumpStatus"], "ScheduledByEarlierPass")
        self.assertEqual(reuse["payloadIds"], [item["payloadId"] for item in
                         json.loads((self.package / "records" / "resource" / "resource-0007.json").read_text(encoding="utf-8"))["subresourceLayouts"]])

    def test_completion_vs_scheduling_and_converted_bc(self) -> None:
        add_instrumented_sidecars(self.dump)
        self.build("--force")
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        no_completion = next(p for p in inventory["payloads"]
                             if p["originalPath"].startswith("Resources/00010000000000000024"))
        self.assertEqual(no_completion["layout"]["DumpStatus"], "ScheduledForReadback")
        self.assertFalse(no_completion["completion"]["observed"])
        self.assertFalse(no_completion["completion"]["dumped"])
        self.assertIn("completion is unobserved", no_completion["completion"]["reason"])
        bc7 = next(p for p in inventory["payloads"]
                   if p["originalPath"].startswith("Resources/00010000000000000023"))
        self.assertEqual(bc7["layout"]["NativeFormat"], "PF_BC7")
        self.assertEqual(bc7["layout"]["StoredFormat"], "PF_FloatRGBA")
        self.assertTrue(bc7["decode"]["supported"])
        self.assertEqual(bc7["decode"]["bytesPerTexel"], 8)
        self.assertFalse(bc7["layout"]["BitExact"])
        self.assertFalse(bc7["layout"]["ValuePreserving"])
        coverage = json.loads((self.package / "coverage.json").read_text(encoding="utf-8"))
        kinds = {g["kind"] for g in coverage["gaps"]}
        self.assertIn("readback-scheduled-not-completed", kinds)
        categories = {c["id"]: c for c in coverage["categories"]}
        self.assertGreaterEqual(categories["missing-bytes"]["count"], 1)
        self.assertGreaterEqual(categories["unproven-native-timing"]["count"], 1)

    def test_unknown_sidecar_fields_are_preserved(self) -> None:
        add_instrumented_sidecars(self.dump)
        self.build("--force")
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        volume = next(p for p in inventory["payloads"]
                      if p["originalPath"].startswith("Resources/00010000000000000020"))
        self.assertEqual(volume["layout"]["FutureExporterField"], {"nested": ["retained", 7], "unknown": True})
        self.assertEqual(volume["completion"]["statusRecord"]["FutureStatusField"], "retained-completion-field")
        pass_record = json.loads((self.package / "records" / "pass" / "pass-0004.json").read_text(encoding="utf-8"))
        self.assertEqual(pass_record["nativeUniformBuffers"][0]["raw"]["FutureBindingRecordField"], {"retained": True})
        parameter_record = json.loads((self.package / "records" / "pass" / "pass-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(parameter_record["parameterBindings"][0]["Bindings"]["SyntheticBufferUAV"]["FutureParameterBindingField"], "retained")
        ref = pass_record["referencedStructs"][0]
        self.assertEqual(ref["FutureReferencedStructField"], ["retained", 1, 2])
        self.assertEqual(ref["ArrayNumElements"], 2)
        self.assertEqual(ref["ContentKind"], "RDGUniformBufferParameters")
        # The native sidecar is retained byte-for-byte, including unknown fields.
        for name in ("SubresourceLayouts.json", "DumpReadbackStatus.json",
                     "NativeUniformBufferBindings.json", "ParameterBindings.json",
                     "ReferencedStructs.json"):
            self.assertEqual((self.package / "native" / "Base" / name).read_bytes(),
                             (self.dump / "Base" / name).read_bytes(), name)

    def test_descriptive_storage_format_never_extracts(self) -> None:
        self.schema.write_text(json.dumps({
            "schemaVersion": 1,
            "resourceSchemas": [{
                "id": "synthetic.descriptive-format",
                "selectors": [{"resourceNameRegex": r"Lumen\.Synthetic\.DirectLighting"}],
                "storage": {
                    "kind": "texture2d",
                    "format": "Synthetic::GetLightingFormat() = GetLightingDataFormat (R11G11B10 in this capture)",
                    "strideBytes": 4,
                    "formatAlternatives": ["PF_FloatRGBA"],
                },
                "fields": [
                    {"name": "looksPacked", "type": "float11", "byteOffset": 0,
                     "bitOffset": 0, "bitWidth": 11, "decode": "R11G11B10 channel r"},
                ],
            }],
        }), encoding="utf-8")
        self.build("--force")
        record = json.loads((self.package / "records" / "resource" / "resource-0001.json").read_text(encoding="utf-8"))
        match = record["schemaMatches"][0]
        self.assertIsNone(match["gateFormat"])
        self.assertIsNone(match["declaredFormat"])
        self.assertIn("GetLightingFormat", match["declaredFormatText"])
        self.assertEqual(match["formatProof"], "descriptive-only")
        self.assertFalse(match["formatMatch"])
        self.assertFalse(match["primaryFieldsExtractable"])
        self.assertFalse(match["extractionEligible"])
        self.assertTrue(match["fields"], "fields must remain visible for inspection")
        self.assertTrue(match["extractionEligibleReason"])
        coverage = json.loads((self.package / "coverage.json").read_text(encoding="utf-8"))
        self.assertIn("resource-schema-format-proof-missing", {g["kind"] for g in coverage["gaps"]})


    def test_empty_pass_draw_counts_stay_null_and_explicit(self) -> None:
        (self.dump / "Base" / "PassDrawCounts.json").write_bytes(b"")
        self.build()
        passes = []
        for path in sorted((self.package / "records" / "pass").glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            passes.append(record)
            self.assertIsNone(record["drawCount"], record["id"])
            self.assertEqual(record["drawCountStatus"], "not-captured")
        self.assertEqual(len(passes), 3)
        self.assertFalse(any(record["drawCount"] == 0 for record in passes))
        coverage = json.loads((self.package / "coverage.json").read_text(encoding="utf-8"))
        kinds = {gap["kind"] for gap in coverage["gaps"]}
        self.assertIn("pass-draw-counts-empty", kinds)
        self.assertEqual(coverage["counts"]["passDrawCountsCaptured"], 0)
        self.assertEqual(coverage["counts"]["passDrawCountsMissing"], 3)
        passes_doc = json.loads((self.package / "passes.json").read_text(encoding="utf-8"))
        self.assertTrue(all(item["drawCount"] is None for item in passes_doc["items"]))
        self.assertTrue(all(item["drawCountStatus"] == "not-captured" for item in passes_doc["items"]))

    def test_viewport_applicability_is_explicit_and_domain_specific(self) -> None:
        add_viewport_fixture(self.dump)
        self.build("--force")
        resources = json.loads((self.package / "resources.json").read_text(encoding="utf-8"))["items"]
        index = json.loads((self.package / "index.json").read_text(encoding="utf-8"))
        by_name: dict[str, list[dict]] = {}
        for item in resources:
            by_name.setdefault(item["name"], []).append(item)

        def one(name: str) -> dict:
            self.assertIn(name, by_name)
            return by_name[name][0]

        # The actual screen target keeps an explicit camera mapping: 128x72
        # valid view inside the 128x96 allocation, active mip 0 bounds match.
        gbuffer = one("GBufferA")
        viewport = gbuffer["viewport"]
        self.assertEqual(viewport["applicability"], "screen-aligned")
        self.assertTrue(viewport["screenPadding"])
        self.assertTrue(viewport["cropAllowed"])
        self.assertTrue(viewport["passIsCameraPass"])
        self.assertEqual(viewport["passViewRect"]["size"], [128, 72])
        self.assertEqual(viewport["activeMip"], 0)
        self.assertEqual(viewport["mipExtent"], [128, 96])
        self.assertTrue(viewport["boundsMatch"])
        self.assertEqual(viewport["sameViewPayload"], "Resources/00010000000000000121.mip0.v00010000000000000111.bin")
        self.assertEqual(gbuffer["padding"]["validWidth"], 128)
        self.assertEqual(gbuffer["padding"]["validHeight"], 72)
        self.assertEqual(gbuffer["padding"]["bottom"], 24)
        self.assertTrue(gbuffer["padding"]["padded"])
        # Same-size known sceneColor and sceneDepth are mapped too; the pass is
        # identified by its screen outputs and captured View.View rect, not by
        # the largest allocation.
        for name in ("SceneColor", "SceneDepthZ"):
            self.assertEqual(one(name)["viewport"]["applicability"], "screen-aligned", name)
        camera_names = {item["name"] for item in index["viewports"]["cameraPasses"]}
        self.assertEqual(camera_names, {"DepthPassParallel", "BasePassParallel"})
        self.assertNotIn("LightFunctionAtlas Generation", camera_names)
        self.assertEqual(index["viewports"]["applicabilityCounts"].get("screen-aligned"), 3)

        # Domain-specific resources retain the pass ViewRect as context only and
        # are never classified as screen padding.
        for name, domain in (
            ("Lumen.SceneAlbedo", "texture2d"),
            ("Lumen.RadianceCache.RadianceProbeIndirectionTexture", "volume-3d"),
            ("CubeBlackDummy", "cube"),
            ("Shadow.Virtual.PhysicalPagePool", "array"),
            ("StochasticLighting.DepthHistory", "texture2d"),
            ("LightFunction.Atlas", "texture2d"),
        ):
            item = one(name)
            vp = item["viewport"]
            self.assertFalse(vp["screenPadding"], name)
            self.assertFalse(vp["cropAllowed"], name)
            self.assertIsNone(item["padding"], name)
            self.assertEqual(vp["domain"], domain, name)
            self.assertTrue(vp["passViewRect"], name)
            self.assertNotEqual(vp["applicability"], "screen-aligned", name)
        self.assertEqual(one("Lumen.SceneAlbedo")["viewport"]["applicability"], "unknown-mapping")
        card = one("Lumen.SceneAlbedo")
        self.assertEqual(card["extent"][:2], [4096, 4096])
        self.assertEqual(card["viewport"]["passViewRect"]["size"], [128, 72])
        self.assertIn("ViewRect retained as context only", card["viewport"]["basis"])
        self.assertEqual(one("Lumen.RadianceCache.RadianceProbeIndirectionTexture")["viewport"]["applicability"],
                         "other-domain")
        self.assertEqual(one("CubeBlackDummy")["viewport"]["classification"], None)
        self.assertEqual(one("Shadow.Virtual.PhysicalPagePool")["viewport"]["classification"], "virtual-shadow-map")
        self.assertEqual(one("StochasticLighting.DepthHistory")["viewport"]["classification"], "history")
        self.assertEqual(one("LightFunction.Atlas")["viewport"]["classification"], "atlas")

        # The raw sidecar layout is retained byte-for-byte: the packaged record
        # adds applicability metadata but changes no bounds, pitch, stride,
        # format, component or unknown field.
        record = json.loads((self.package / gbuffer["record"]).read_text(encoding="utf-8"))
        raw_layouts = parse_json_sequence(self.dump / "Base" / "SubresourceLayouts.json")
        raw = next(item for item in raw_layouts if item.get("Resource") == "00010000000000000121")
        self.assertEqual(record["subresourceLayouts"][0]["layout"], raw)
        self.assertEqual(record["subresourceLayouts"][0]["layout"]["FutureViewportLayoutField"],
                         {"retained": True})
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        payload = next(item for item in inventory["payloads"]
                       if item["originalPath"] == viewport["sameViewPayload"])
        self.assertEqual(payload["layout"], raw)
        self.assertEqual(payload["bytes"], raw["ByteSize"])
        self.assertEqual(payload["sha256"], sha256(self.dump / raw["File"]))
        self.assertEqual(sum(chunk["length"] for chunk in payload["chunks"]), payload["bytes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
