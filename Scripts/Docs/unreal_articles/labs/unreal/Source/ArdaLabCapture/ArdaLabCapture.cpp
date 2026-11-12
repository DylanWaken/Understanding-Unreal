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
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Components/InstancedStaticMeshComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Animation/AnimationAsset.h"
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
    else if (Mode == "wireframe") Client.SetViewMode(VMI_Wireframe);
    else if (Mode == "unlit") Client.SetViewMode(VMI_Unlit);
    else Client.SetViewMode(VMI_Lit);
    Client.Invalidate();
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
