# The same room, extended for scene representation

This experiment loads `/Game/Labs/LumenRoom`, retains all seven original mesh actors and the original ceiling light, then adds three actors: a hinged door, instanced potted foliage, and Epic's skeletal mannequin. It saves `/Game/Labs/SceneRepresentationRoom`; the original map and Lumen screenshots remain unchanged.

## Reproduce

After running `capture.py` to create the baseline room:

```powershell
./Scripts/Docs/unreal_articles/labs/unreal/run.ps1 -Build -Script capture_scene_room.py
python Scripts/Docs/unreal_articles/labs/unreal/publish_scene_room.py `
  "$env:LOCALAPPDATA/Temp/arda-unreal-lab" `
  Docs/Unreal/assets/scene-room-captures
```

The runner copies the locally installed Standard Mannequin template into the temporary project's `Content/Mannequin` if needed. No template assets, Unreal binaries, or project map binaries are committed. The publisher verifies screenshot hashes, attachment transforms, instance counts, the skeletal asset, and the unchanged original room before copying PNGs. It records the GPU, engine revision, template input hashes, and reproduction source hashes.

To check the published artifacts without launching Unreal:

```powershell
python Scripts/Docs/unreal_articles/labs/unreal/publish_scene_room.py unused `
  Docs/Unreal/assets/scene-room-captures --check-published
```

## Concrete inventory

Coordinates are Unreal centimeters, using the same `UE = (-100x, 100y, 100z)` mapping as the baseline room.

- **Door:** actor with a movable `USceneComponent` root at `(-150,200,0)`. `DoorPanel` uses the shared engine cube, relative location `(50,0,100)`, scale `(1,.06,2)`: a 100 × 6 × 200 cm panel. `AttachedIndicator` uses the engine sphere, relative location `(60,0,150)`, scale `(.1,.1,.1)`. The root rotates from yaw 0 to +90°. The indicator center therefore changes from `(-90,200,150)` to `(-150,260,150)`. Both meshes are genuine child components. The indicator is emissive geometry, not a point light.
- **Foliage:** one actor with three `UInstancedStaticMeshComponent`s: three cylinder pots, three cylinder stems, and 24 planar leaf instances (eight per pot). Pot centers are `(-145,80)`, `(-105,80)`, and `(-65,80)`. The leaves share a masked, two-sided foliage material with an analytic UV sphere mask. The leaf silhouette comes from the material; wireframe reveals the underlying rectangles. There is no wind/WPO in these captures, and the instance count is not a draw-call measurement.
- **Character:** one `ASkeletalMeshActor` at `(65,110,0)`, yaw -90°. It uses the real Epic Standard Mannequin `SK_Mannequin` with 68 bones. The same `ThirdPersonWalk` sequence is evaluated at 0.10 s and 0.45 s with the component transform held fixed. These are frozen samples of actual skeletal animation, not an animation-controller, cloth, root-motion, or physics demonstration.

The scene contains 13 visible mesh components: seven original static components, two door static components, three instancing components, and one skeletal component. This excludes editor visualization helpers and does not claim 13 draws or 13 GPU Scene instance entries. Actual actor/component paths, transforms, bounds, material references, and all instance transforms are in `scene-room-inventory.json`. Runtime object paths still reflect the loaded baseline package; `/Game/Labs/SceneRepresentationRoom` is the separately saved result.

## Images and camera semantics

All 12 PNGs are 1280 × 720 actual Unreal editor viewport captures, without compositing or image editing. Overview images reuse the original camera `(0,-400,165)` looking toward `(0,280,140)`. Detail views move only the same camera; positions and targets are in `scene-room-capture-report.json`. Vertical FOV is 43°, fixed exposure EV100=4, and the original ceiling area light is 2400 lumens. Lumen software tracing and screen traces remain enabled. Stochastic lighting noise is visible.

`room-extended-lit.png` and `room-extended-moved.png` hold the overview camera fixed while the hinge rotates and the skeletal pose changes. The four buffer/debug overview images show native BaseColor, WorldNormal, wireframe, and bounds modes. The foliage pair shows masked coverage versus triangles. The two mannequin views hold the character's component transform fixed while skinning changes the mesh. The two door views hold the camera fixed while both parent-child world transforms and lighting change. A changed screenshot is not a GPU timing, GI convergence, or velocity-buffer measurement.

Epic's mannequin appearance is rendered from local Unreal template assets. The meshes, skeleton, animations, and materials themselves are not redistributed here. See the source paths and input hashes in `scene-room-provenance.json`.
