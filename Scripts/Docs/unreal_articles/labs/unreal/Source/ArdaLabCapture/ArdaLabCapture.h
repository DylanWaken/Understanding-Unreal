#pragma once
#include "Kismet/BlueprintFunctionLibrary.h"
#include "ArdaLabCapture.generated.h"
class UMaterialExpression;
class UStaticMesh;
class USceneComponent;
class UStaticMeshComponent;
class UInstancedStaticMeshComponent;
class UMaterialInterface;
class USkeletalMeshComponent;
class UAnimationAsset;
UCLASS()
class UArdaLabCapture : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    UFUNCTION(BlueprintCallable) static void SetViewportMode(FName Mode, FName Buffer);
    UFUNCTION(BlueprintCallable) static bool ConnectDisplacement(UMaterialExpression* Expression);
    UFUNCTION(BlueprintCallable) static FString NaniteStats(UStaticMesh* Mesh);
    UFUNCTION(BlueprintCallable) static void RenderViewport();
    UFUNCTION(BlueprintCallable) static void CaptureViewport(const FString& Filename);
    UFUNCTION(BlueprintCallable) static USceneComponent* MakeSceneRoot(AActor* Actor);
    UFUNCTION(BlueprintCallable) static UStaticMeshComponent* AddMeshPart(AActor* Actor, FName Name, UStaticMesh* Mesh, UMaterialInterface* Material, FTransform RelativeTransform);
    UFUNCTION(BlueprintCallable) static UInstancedStaticMeshComponent* AddInstanceGroup(AActor* Actor, FName Name, UStaticMesh* Mesh, UMaterialInterface* Material, const TArray<FTransform>& Transforms);
    UFUNCTION(BlueprintCallable) static void SetFrozenPose(USkeletalMeshComponent* Component, UAnimationAsset* Animation, float Seconds);
};
