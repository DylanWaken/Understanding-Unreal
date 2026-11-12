using UnrealBuildTool;
public class UnrealLabEditorTarget : TargetRules
{
    public UnrealLabEditorTarget(TargetInfo Target) : base(Target)
    {
        Type = TargetType.Editor;
        DefaultBuildSettings = BuildSettingsVersion.Latest;
        IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
        ExtraModuleNames.Add("ArdaLabCapture");
    }
}
