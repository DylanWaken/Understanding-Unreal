using UnrealBuildTool;
public class ArdaLabCapture : ModuleRules
{
    public ArdaLabCapture(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Engine" });
        PrivateDependencyModuleNames.AddRange(new[] { "UnrealEd", "LevelEditor", "Slate", "SlateCore", "MaterialEditor", "RenderCore", "RHI" });
    }
}
