#include "ArdaLabCapture.h"
#include "Modules/ModuleManager.h"
#include "LevelEditor.h"
#include "SLevelViewport.h"
#include "LevelEditorViewport.h"
#include "MaterialEditingLibrary.h"
#include "Materials/MaterialExpression.h"
#include "Engine/StaticMesh.h"
#include "StaticMeshResources.h"
#include "Rendering/NaniteResources.h"
#include "Slate/SceneViewport.h"
#include "UnrealClient.h"
#include "RHIGlobals.h"
#include "DataDrivenShaderPlatformInfo.h"
#include "SceneManagement.h"
#include "SceneTypes.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Components/InstancedStaticMeshComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Animation/AnimationAsset.h"
#include "Engine/Engine.h"
#include "Editor.h"
#include "DynamicRHI.h"
#include "HAL/IConsoleManager.h"
IMPLEMENT_MODULE(FDefaultModuleImpl, ArdaLabCapture)

void UArdaLabCapture::RenderViewport()
{
    auto Viewport = FModuleManager::LoadModuleChecked<FLevelEditorModule>("LevelEditor").GetFirstActiveLevelViewport();
    if (!Viewport.IsValid()) return;
    auto SceneViewport = Viewport->GetSharedActiveViewport();
    SceneViewport->SetFixedViewportSize(1280, 720);
    SceneViewport->Draw(false);
}
void UArdaLabCapture::CaptureViewport(const FString& Filename)
{
    FScreenshotRequest::RequestScreenshot(Filename, false, false);
    RenderViewport();
}

void UArdaLabCapture::SetViewportMode(FName Mode, FName Buffer)
{
    auto Viewport = FModuleManager::LoadModuleChecked<FLevelEditorModule>("LevelEditor").GetFirstActiveLevelViewport();
    if (!Viewport.IsValid()) return;
    auto& Client = Viewport->GetLevelViewportClient();
    if (Mode == "buffer") { Client.SetViewMode(VMI_VisualizeBuffer); Client.ChangeBufferVisualizationMode(Buffer); }
    else if (Mode == "vsm") { Client.ChangeVirtualShadowMapVisualizationMode(Buffer); }
    else if (Mode == "wireframe") Client.SetViewMode(VMI_Wireframe);
    else if (Mode == "unlit") Client.SetViewMode(VMI_Unlit);
    else if (Mode == "pathtracing") Client.SetViewMode(VMI_PathTracing);
    else Client.SetViewMode(VMI_Lit);
    Client.Invalidate();
}

namespace
{
    FSceneViewStateInterface* GetActiveViewState()
    {
        auto Viewport = FModuleManager::LoadModuleChecked<FLevelEditorModule>("LevelEditor").GetFirstActiveLevelViewport();
        if (!Viewport.IsValid()) return nullptr;
        return Viewport->GetLevelViewportClient().ViewState.GetReference();
    }
}

int32 UArdaLabCapture::GetPathTracingSamples()
{
#if RHI_RAYTRACING
    FSceneViewStateInterface* State = GetActiveViewState();
    return State ? static_cast<int32>(State->GetPathTracingSampleIndex()) : -1;
#else
    return -1;
#endif
}

int32 UArdaLabCapture::GetPathTracingSampleCount()
{
#if RHI_RAYTRACING
    FSceneViewStateInterface* State = GetActiveViewState();
    return State ? static_cast<int32>(State->GetPathTracingSampleCount()) : -1;
#else
    return -1;
#endif
}

FString UArdaLabCapture::GetPathTracingStatus()
{
    int32 ViewModeIndex = -1;
    bool bPathTracingViewMode = false;
    auto Viewport = FModuleManager::LoadModuleChecked<FLevelEditorModule>("LevelEditor").GetFirstActiveLevelViewport();
    if (Viewport.IsValid())
    {
        ViewModeIndex = static_cast<int32>(Viewport->GetLevelViewportClient().GetViewMode());
        bPathTracingViewMode = ViewModeIndex == static_cast<int32>(VMI_PathTracing);
    }
    return FString::Printf(
        TEXT("{\"view_mode_index\":%d,\"is_path_tracing_view_mode\":%s,\"ray_tracing_supported\":%s,\"ray_tracing_shaders_supported\":%s,\"path_tracing_shader_support\":%s,\"samples\":%d,\"sample_count\":%d}"),
        ViewModeIndex,
        bPathTracingViewMode ? TEXT("true") : TEXT("false"),
        GRHISupportsRayTracing ? TEXT("true") : TEXT("false"),
        GRHISupportsRayTracingShaders ? TEXT("true") : TEXT("false"),
        FDataDrivenShaderPlatformInfo::GetSupportsPathTracing(GMaxRHIShaderPlatform) ? TEXT("true") : TEXT("false"),
        GetPathTracingSamples(),
        GetPathTracingSampleCount());
}

namespace
{
    FString JsonEscape(const FString& In)
    {
        FString S = In;
        S.ReplaceInline(TEXT("\\"), TEXT("\\\\"));
        S.ReplaceInline(TEXT("\""), TEXT("\\\""));
        S.ReplaceInline(TEXT("\r"), TEXT("\\r"));
        S.ReplaceInline(TEXT("\n"), TEXT("\\n"));
        S.ReplaceInline(TEXT("\t"), TEXT("\\t"));
        return S;
    }
}

FString UArdaLabCapture::GetConsoleVariable(FString Name)
{
    IConsoleVariable* CVar = IConsoleManager::Get().FindConsoleVariable(*Name);
    if (!CVar)
    {
        return FString::Printf(TEXT("{\"name\":\"%s\",\"found\":false}"), *JsonEscape(Name));
    }
    const TCHAR* SetByName = GetConsoleVariableSetByName((EConsoleVariableFlags)((uint32)CVar->GetFlags() & (uint32)ECVF_SetByMask));
    return FString::Printf(
        TEXT("{\"name\":\"%s\",\"found\":true,\"value\":\"%s\",\"default\":\"%s\",\"set_by\":\"%s\",\"flags\":%u}"),
        *JsonEscape(Name),
        *JsonEscape(CVar->GetString()),
        *JsonEscape(CVar->GetDefaultValue()),
        SetByName ? SetByName : TEXT("unknown"),
        (uint32)CVar->GetFlags());
}

FString UArdaLabCapture::GetConsoleVariables(FString Prefix)
{
    FString Out = TEXT("[");
    bool bFirst = true;
    IConsoleManager::Get().ForEachConsoleObjectThatStartsWith(
        FConsoleObjectVisitor::CreateLambda([&Out, &bFirst](const TCHAR* CVarName, IConsoleObject* ConsoleObj)
        {
            if (ConsoleObj->TestFlags(ECVF_Unregistered))
            {
                return;
            }
            IConsoleVariable* CVar = ConsoleObj->AsVariable();
            if (!CVar)
            {
                return;
            }
            const TCHAR* Type = nullptr;
            if (CVar->IsVariableBool()) Type = TEXT("bool");
            else if (CVar->IsVariableInt()) Type = TEXT("int32");
            else if (CVar->IsVariableFloat()) Type = TEXT("float");
            else if (CVar->IsVariableString()) Type = TEXT("FString");
            if (!Type)
            {
                return;
            }
            const TCHAR* SetByName = GetConsoleVariableSetByName(CVar->GetFlags());
            if (!bFirst) Out += TEXT(",");
            bFirst = false;
            Out += FString::Printf(
                TEXT("{\"name\":\"%s\",\"type\":\"%s\",\"value\":\"%s\",\"default\":\"%s\",\"set_by\":\"%s\"}"),
                *JsonEscape(FString(CVarName)),
                Type,
                *JsonEscape(CVar->GetString()),
                *JsonEscape(CVar->GetDefaultValue()),
                SetByName ? SetByName : TEXT("unknown"));
        }),
        *Prefix);
    Out += TEXT("]");
    return Out;
}

FString UArdaLabCapture::GetRenderStatus()
{
    int32 ViewModeIndex = -1;
    FIntPoint ViewportSize(0, 0);
    bool bSceneHDREnabled = false;
    auto Viewport = FModuleManager::LoadModuleChecked<FLevelEditorModule>("LevelEditor").GetFirstActiveLevelViewport();
    if (Viewport.IsValid())
    {
        ViewModeIndex = static_cast<int32>(Viewport->GetLevelViewportClient().GetViewMode());
        if (auto SceneViewport = Viewport->GetSharedActiveViewport())
        {
            ViewportSize = SceneViewport->GetSizeXY();
            bSceneHDREnabled = SceneViewport->GetSceneHDREnabled();
        }
    }
    const FString RHIName = GDynamicRHI ? GDynamicRHI->GetName() : TEXT("");
    return FString::Printf(
        TEXT("{\"rhi\":\"%s\",\"feature_level\":%d,\"shader_platform\":\"%s\",\"view_mode_index\":%d,")
        TEXT("\"viewport_size\":[%d,%d],\"scene_hdr_enabled\":%s,\"ray_tracing_supported\":%s,")
        TEXT("\"ray_tracing_shaders_supported\":%s,\"path_tracing_shader_support\":%s}"),
        *JsonEscape(RHIName),
        (int32)GMaxRHIFeatureLevel,
        *JsonEscape(LexToString(GMaxRHIShaderPlatform)),
        ViewModeIndex,
        ViewportSize.X,
        ViewportSize.Y,
        bSceneHDREnabled ? TEXT("true") : TEXT("false"),
        GRHISupportsRayTracing ? TEXT("true") : TEXT("false"),
        GRHISupportsRayTracingShaders ? TEXT("true") : TEXT("false"),
        FDataDrivenShaderPlatformInfo::GetSupportsPathTracing(GMaxRHIShaderPlatform) ? TEXT("true") : TEXT("false"));
}

bool UArdaLabCapture::ExecConsoleCommand(FString Command)
{
    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    const bool bExecuted = GEngine && GEngine->Exec(World, *Command);
    UE_LOG(LogTemp, Display, TEXT("ARDA_GPUDUMP_COMMAND executed=%d %s"), bExecuted ? 1 : 0, *Command);
    return bExecuted;
}

bool UArdaLabCapture::ConnectDisplacement(UMaterialExpression* Expression)
{
    return UMaterialEditingLibrary::ConnectMaterialProperty(Expression, FString(), MP_Displacement);
}
FString UArdaLabCapture::NaniteStats(UStaticMesh* Mesh)
{
    if (!Mesh || !Mesh->GetRenderData() || !Mesh->GetRenderData()->NaniteResourcesPtr) return "{}";
    const auto& R = *Mesh->GetRenderData()->NaniteResourcesPtr;
    return FString::Printf(TEXT("{\"input_triangles\":%u,\"input_vertices\":%u,\"clusters_all_lods\":%u,\"root_pages\":%u,\"pages\":%d,\"hierarchy_nodes\":%d,\"root_data_bytes\":%d,\"streamable_data_bytes\":%lld,\"position_precision\":%d}"),
        R.NumInputTriangles, R.NumInputVertices, R.NumClusters, R.NumRootPages, R.PageStreamingStates.Num(), R.HierarchyNodes.Num(), R.RootData.Num(), R.StreamablePages.GetBulkDataSize(), R.PositionPrecision);
}

USceneComponent* UArdaLabCapture::MakeSceneRoot(AActor* Actor)
{
    if (!Actor) return nullptr;
    if (Actor->GetRootComponent()) return Actor->GetRootComponent();
    auto* Root = NewObject<USceneComponent>(Actor, TEXT("TeachingRoot"));
    Actor->AddInstanceComponent(Root);
    Actor->SetRootComponent(Root);
    Root->SetMobility(EComponentMobility::Movable);
    Root->RegisterComponent();
    return Root;
}

UStaticMeshComponent* UArdaLabCapture::AddMeshPart(AActor* Actor, FName Name, UStaticMesh* Mesh, UMaterialInterface* Material, FTransform RelativeTransform)
{
    auto* Root = MakeSceneRoot(Actor);
    if (!Root) return nullptr;
    auto* Part = NewObject<UStaticMeshComponent>(Actor, Name);
    Actor->AddInstanceComponent(Part);
    Part->SetupAttachment(Root);
    Part->SetMobility(EComponentMobility::Movable);
    Part->SetStaticMesh(Mesh);
    Part->SetMaterial(0, Material);
    Part->SetRelativeTransform(RelativeTransform);
    Part->RegisterComponent();
    return Part;
}

UInstancedStaticMeshComponent* UArdaLabCapture::AddInstanceGroup(AActor* Actor, FName Name, UStaticMesh* Mesh, UMaterialInterface* Material, const TArray<FTransform>& Transforms)
{
    auto* Root = MakeSceneRoot(Actor);
    if (!Root) return nullptr;
    auto* Group = NewObject<UInstancedStaticMeshComponent>(Actor, Name);
    Actor->AddInstanceComponent(Group);
    Group->SetupAttachment(Root);
    Group->SetMobility(EComponentMobility::Movable);
    Group->SetStaticMesh(Mesh);
    Group->SetMaterial(0, Material);
    Group->RegisterComponent();
    for (const auto& Transform : Transforms) Group->AddInstance(Transform);
    return Group;
}

void UArdaLabCapture::SetFrozenPose(USkeletalMeshComponent* Component, UAnimationAsset* Animation, float Seconds)
{
    if (!Component || !Animation) return;
    Component->SetAnimationMode(EAnimationMode::AnimationSingleNode);
    Component->SetAnimation(Animation);
    Component->SetPosition(Seconds, false);
    Component->Stop();
    Component->TickAnimation(0.f, false);
    Component->RefreshBoneTransforms();
    Component->UpdateBounds();
    Component->MarkRenderTransformDirty();
    Component->MarkRenderDynamicDataDirty();
}
