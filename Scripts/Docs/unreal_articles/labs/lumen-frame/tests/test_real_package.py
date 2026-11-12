#!/usr/bin/env python3
"""Optional checks against the built real package (set FRAME_PACKAGE_DIR).

These checks are skipped when no package is available.  They do not rebuild or
modify the package; the archive/extraction is supplied by the caller.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

PACKAGE = Path(os.environ.get("FRAME_PACKAGE_DIR", "")).resolve() if os.environ.get("FRAME_PACKAGE_DIR") else None
if PACKAGE is None:
    PACKAGE = next((parent / 'Docs/Unreal/assets/lumen-frame' for parent in Path(__file__).resolve().parents
                    if (parent / 'Docs/Unreal/assets/lumen-frame/index.json').is_file()), None)
HAS_PACKAGE = bool(PACKAGE and (PACKAGE / "index.json").exists())


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


@unittest.skipUnless(HAS_PACKAGE, "set FRAME_PACKAGE_DIR to an extracted real package")
class RealPackageViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert PACKAGE is not None
        cls.package = PACKAGE
        cls.index = json.loads((PACKAGE / "index.json").read_text(encoding="utf-8"))
        cls.resources = json.loads((PACKAGE / "resources.json").read_text(encoding="utf-8"))["items"]

    def resource(self, unique_name: str) -> dict:
        return next(item for item in self.resources if item["uniqueName"] == unique_name)

    def test_actual_camera_screen_targets_are_croppable(self) -> None:
        # GBufferA for the first captured frame: 1280x888 allocation, 1280x720
        # camera ViewRect.  The active mip-0 sidecar matches the descriptor.
        gbuffer = self.resource("000100000228d3653720")
        self.assertEqual(gbuffer["name"], "GBufferA")
        self.assertEqual(gbuffer["extent"][:2], [1280, 888])
        viewport = gbuffer["viewport"]
        self.assertEqual(viewport["applicability"], "screen-aligned")
        self.assertTrue(viewport["screenPadding"])
        self.assertTrue(viewport["cropAllowed"])
        self.assertTrue(viewport["passIsCameraPass"])
        self.assertEqual(viewport["passViewRect"]["size"], [1280, 720])
        self.assertEqual(viewport["activeMip"], 0)
        self.assertEqual(viewport["mipExtent"], [1280, 888])
        self.assertTrue(viewport["boundsMatch"])
        self.assertEqual(gbuffer["padding"]["validWidth"], 1280)
        self.assertEqual(gbuffer["padding"]["validHeight"], 720)
        self.assertEqual(gbuffer["padding"]["bottom"], 168)
        self.assertTrue(gbuffer["padding"]["padded"])
        # Actual sceneColor and sceneDepth are mapped by the same camera output
        # evidence, not by a largest-allocation heuristic.
        for unique_name in ("000100000228d3652960", "000100000228d36523c0"):
            item = self.resource(unique_name)
            self.assertEqual(item["viewport"]["applicability"], "screen-aligned", item["name"])
            self.assertTrue(item["viewport"]["cropAllowed"], item["name"])
            self.assertEqual(item["padding"]["validHeight"], 720, item["name"])
        summary = self.index["viewports"]
        self.assertEqual(summary["applicabilityCounts"].get("screen-aligned"), 18)
        self.assertTrue(all(pas["viewRect"]["size"] == [1280, 720] for pas in summary["cameraPasses"]))
        self.assertTrue(all(pas["name"] in {
            "DepthPassParallel", "EditorPrimitives", "BasePassParallel", "EditorPrimitives: SDPG_World",
        } for pas in summary["cameraPasses"]))
        self.assertNotIn("LightFunctionAtlas Generation", {pas["name"] for pas in summary["cameraPasses"]})

    def test_actual_non_screen_domains_are_not_padding(self) -> None:
        # Every 4096 Lumen scene card atlas must keep its own full-subresource
        # domain, regardless of the camera ViewRect on other resources.
        card_atlases = [item for item in self.resources
                        if item["kind"] == "texture" and item["name"].startswith("Lumen.")
                        and item["extent"][0] == 4096 and item["extent"][1] == 4096]
        self.assertGreaterEqual(len(card_atlases), 1)
        for item in card_atlases:
            viewport = item["viewport"]
            self.assertFalse(viewport["screenPadding"], item["name"])
            self.assertFalse(viewport["cropAllowed"], item["name"])
            self.assertIsNone(item["padding"], item["name"])
        albedo = next(item for item in card_atlases if item["name"] == "Lumen.SceneAlbedo")
        self.assertEqual(albedo["viewport"]["applicability"], "no-view-context")

        volume = self.resource("000100000228dcc20010")
        self.assertEqual(volume["name"], "Lumen.RadianceCache.RadianceProbeIndirectionTexture")
        self.assertEqual(volume["viewport"]["domain"], "volume-3d")
        self.assertFalse(volume["viewport"]["screenPadding"])
        self.assertIsNone(volume["padding"])

        cube = self.resource("000100000228d576f860")
        self.assertEqual(cube["name"], "CubeBlackDummy")
        self.assertEqual(cube["viewport"]["domain"], "cube")
        self.assertFalse(cube["viewport"]["screenPadding"])
        self.assertTrue(cube["viewport"]["passViewRect"])
        self.assertIsNone(cube["padding"])

        vsm = self.resource("000100000228d23ff6c0")
        self.assertEqual(vsm["name"], "Shadow.Virtual.PhysicalPagePool")
        self.assertEqual(vsm["viewport"]["domain"], "array")
        self.assertFalse(vsm["viewport"]["screenPadding"])
        self.assertIsNone(vsm["padding"])

        gdf = self.resource("000100000228d576b738")
        self.assertEqual(gdf["name"], "GlobalDistanceField.PageAtlas")
        self.assertEqual(gdf["viewport"]["domain"], "volume-3d")
        self.assertEqual(gdf["viewport"]["applicability"], "other-domain")
        self.assertTrue(gdf["viewport"]["passViewRect"])
        self.assertFalse(gdf["viewport"]["screenPadding"])
        self.assertIsNone(gdf["padding"])

    def test_raw_subresource_addressing_and_counts_unchanged(self) -> None:
        raw_layouts = parse_json_sequence(self.package / "native" / "Base" / "SubresourceLayouts.json")
        self.assertEqual(len(raw_layouts), 1496)
        raw_by_file = {item["File"]: item for item in raw_layouts}
        self.assertEqual(len(raw_by_file), 1496)
        seen = 0
        for item in self.resources:
            if not item.get("hasSubresourceLayouts"):
                continue
            record = json.loads((self.package / item["record"]).read_text(encoding="utf-8"))
            for layout_item in record["subresourceLayouts"]:
                self.assertEqual(layout_item["layout"], raw_by_file[layout_item["file"]], layout_item["file"])
                seen += 1
        self.assertEqual(seen, 1496)

        counts = self.index["counts"]
        self.assertEqual(counts["resources"], 1449)
        self.assertEqual(counts["passes"], 928)
        self.assertEqual(counts["payloadFiles"], 7024)
        self.assertEqual(counts["payloadBytes"], 18502060278)
        self.assertEqual(counts["textureReadbackPayloads"], 1496)
        self.assertEqual(counts["volumePayloads"], 135)
        self.assertEqual(counts["cubeFacePayloads"], 18)
        self.assertEqual(counts["arrayPayloads"], 210)
        self.assertEqual(counts["nativeBufferSubranges"], 3090)
        self.assertEqual(counts["skipTrackingPayloads"], 24)
        self.assertEqual(counts["rdgBufferVersionsWithoutCompletion"], 1183)
        self.assertEqual(counts["chunkFiles"], 3136)
        self.assertEqual(counts["uniqueChunkBytes"], 1567676477)
        self.assertEqual(counts["compressedChunkBytes"], 252014634)
        self.assertEqual(counts["packageFiles"], 6410)

    def test_all_native_uniform_payloads_resolve_to_final_inventory(self) -> None:
        inventory = json.loads((self.package / "inventory.json").read_text(encoding="utf-8"))
        payloads = {payload["id"]: payload for payload in inventory["payloads"]}
        passes = json.loads((self.package / "passes.json").read_text(encoding="utf-8"))["items"]
        resolved = 0
        chosen = None
        for item in passes:
            record = json.loads((self.package / item["record"]).read_text(encoding="utf-8"))
            for native in record.get("nativeUniformBuffers", []):
                embedded = native.get("uniformBufferPayload")
                if embedded is None:
                    continue
                resolved += 1
                self.assertIn(embedded["id"], payloads, native.get("uniformBuffer"))
                self.assertEqual(embedded, payloads[embedded["id"]], native.get("uniformBuffer"))
                self.assertGreater(embedded["bytes"], 0, native.get("uniformBuffer"))
                self.assertTrue(embedded["sha256"], native.get("uniformBuffer"))
                self.assertTrue(embedded["chunks"], native.get("uniformBuffer"))
                if (record["id"] == "pass-0117"
                        and native.get("uniformBuffer") == "00010000022880566180"):
                    chosen = embedded
        self.assertEqual(resolved, 470)
        self.assertIsNotNone(chosen, "the reviewed pass-0117 uniform payload is missing")
        self.assertEqual(chosen["id"], "payload-05915")
        self.assertEqual(chosen["bytes"], 6752)
        self.assertEqual(chosen["sha256"],
                         "0ce9aefbd724dd7861e95e81a389b12d19a5a62ca0ae534b72d06b2b288c6944")
        self.assertEqual(chosen["chunks"][0]["offset"], 0)
        self.assertEqual(chosen["chunks"][0]["length"], 6752)

    def test_six_descriptive_resampled_card_schemas_never_extract(self) -> None:
        matches = []
        for item in self.resources:
            record = json.loads((self.package / item["record"]).read_text(encoding="utf-8"))
            for match in record.get("schemaMatches") or []:
                if match.get("schemaId") in {
                    "cards.resource.resampled-card-capture-direct",
                    "cards.resource.resampled-card-capture-indirect",
                }:
                    matches.append((item, match))
        self.assertEqual(len(matches), 6)
        for item, match in matches:
            self.assertEqual(match["formatProof"], "descriptive-only", item["name"])
            self.assertIsNone(match["declaredFormat"], item["name"])
            self.assertIsNone(match["gateFormat"], item["name"])
            self.assertIsNone(match["activeFormatVariant"], item["name"])
            self.assertFalse(match["primaryFieldsExtractable"], item["name"])
            self.assertFalse(match["extractionEligible"], item["name"])
            self.assertTrue(match["extractionEligibleReason"], item["name"])
            self.assertGreaterEqual(len(match["fields"]), 3, item["name"])
            self.assertEqual(match["formatVariantFields"], [], item["name"])
            for field in match["fields"]:
                self.assertIn("byteOffset", field, item["name"])
                self.assertIn("name", field, item["name"])
        self.assertEqual({item["name"] for item, _ in matches}, {
            "Lumen.ResampledCardCaptureDirectLighting",
            "Lumen.ResampledCardCaptureIndirectLighting",
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
