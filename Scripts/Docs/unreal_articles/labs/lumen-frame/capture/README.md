# Reproduce the instrumented LumenRoom capture

The final capture is `binary-20261112-153914`, produced by UE 5.8.1 at engine commit `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43` with the diagnostic patch in this directory. It used an NVIDIA RTX PRO 6000 Blackwell Workstation Edition, driver 610.62, D3D12 SM6. The saved LumenRoom map has SHA-256 `f7255018f916af33894b1d0bd811b21585dc0151fca33fddc3a538851337e818`, matching the separately packaged baseline project.

Use an Unreal Engine source checkout at the recorded commit and the portable project in the baseline capture package’s `reproduce/UnrealLab` directory. Apply the tracked-file patch, then place `patch/DumpBuffer.usf.new` at `Engine/Shaders/Private/Tools/DumpBuffer.usf`. From the engine root, check the patch before applying it.

```powershell
git apply --check "<capture-package>/patch/ours-only.patch"
git apply "<capture-package>/patch/ours-only.patch"
Copy-Item -LiteralPath "<capture-package>/patch/DumpBuffer.usf.new" -Destination "Engine/Shaders/Private/Tools/DumpBuffer.usf"
& .\Engine\Build\BatchFiles\Build.bat UnrealEditor Win64 Development
& .\Engine\Build\BatchFiles\Build.bat UnrealLabEditor Win64 Development -Project="<project-root>/UnrealLab.uproject"
```

Run the capture wrapper with explicit paths.

```powershell
& "<capture-package>/reproduce/run_full_capture.ps1" -EngineRoot "<engine-root>" -ProjectRoot "<project-root>" -OutputRoot "<capture-output>"
```

The wrapper starts UniformBufferContentMap before uniform creation and the Python script requests one unfiltered DumpGPU engine frame with native textures, SkipTracking buffers, pass parameters and CVars enabled. It uses 1280×720, software Lumen, VSM and histogram exposure fixed at EV100 4. It records 200 warmup and 90 settle draw requests before the dump. The final capture contained three scene graphs inside one engine frame. Exact pointer IDs and temporal state can vary across runs. Keep each dump’s resource files and metadata together.

The original capture used a copied, already compiled project module from the reference scene and aligned the copied manifest’s BuildId. These commands build the portable project against the selected engine. They were syntax-checked locally. The captured run and renderer build are separately verified by the accompanying evidence.

`patch/build-products.txt` records the final binary/source hashes. `patch/native-module-build-7.log` is the successful compile and link of the exact RenderCore module used for this capture. `patch/ours-only.patch` applies to the two previously tracked source files. The new shader is carried separately with its own hash.
