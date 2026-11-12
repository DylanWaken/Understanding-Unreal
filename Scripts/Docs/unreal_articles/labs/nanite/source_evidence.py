"""Freeze the exact Unreal source ranges cited by the accompanying lab chapter."""
from pathlib import Path
import hashlib,json

HERE=Path(__file__).resolve().parent
ENGINE=Path('D:/UnrealEngine')
REVISION='71fe36aac5a8df5ccd66c763ffc902b29b6a9c43'
RECORDS=[
('lab-cluster-capacity','Engine/Shaders/Shared/NaniteDefinitions.h',21,28,'The inspected triangle path caps a cluster at 128 triangles and 256 vertices.'),
('lab-builder-cluster','Engine/Source/Developer/NaniteBuilder/Private/Cluster.h',272,327,'Builder FCluster stores geometry, external edges, bounds/errors, group relationships, and encoding work.'),
('lab-builder-group','Engine/Source/Developer/NaniteBuilder/Private/ClusterDAG.h',33,47,'FClusterGroup carries children, bounds, parent error, page-range association and mesh/assembly state.'),
('lab-reduce-group','Engine/Source/Developer/NaniteBuilder/Private/ClusterDAG.cpp',1375,1602,'ReduceGroup merges children, simplifies, repartitions parents and propagates shared parent error.'),
('lab-simplifier-settings','Engine/Source/Developer/NaniteBuilder/Private/Cluster.cpp',965,1052,'The builder supplies attribute weights, locks external edge positions, and configures the mesh simplifier.'),
('lab-packed-cluster','Engine/Source/Runtime/Engine/Public/Rendering/NaniteResources.h',105,144,'FPackedCluster groups geometry decoding, culling and material-access metadata into the GPU record.'),
('lab-selection-shader','Engine/Shaders/Private/Nanite/NaniteClusterCulling.usf',256,312,'Hierarchy traversal and cluster draw eligibility use projected edge scales, stored errors and view/instance scale conventions.'),
('lab-hierarchy-slice','Engine/Shaders/Private/Nanite/NaniteDataDecode.ush',171,188,'FHierarchyNodeSlice exposes bounds, error interval, child/page references, loaded/enabled flags and deformation metadata.'),
('lab-streaming-state','Engine/Source/Runtime/Engine/Public/Rendering/NaniteResources.h',248,258,'FPageStreamingState records storage sizes and offsets, dependencies, hierarchy depth and flags.'),
('lab-instance-postpass','Engine/Shaders/Private/Nanite/NaniteInstanceCulling.usf',353,421,'The instance-culling shader distinguishes previous-frame/main-pass occlusion from post-pass recovery.'),
('lab-visible-cluster','Engine/Shaders/Private/Nanite/NaniteDataDecode.ush',48,60,'FVisibleCluster identifies the view, instance, page and cluster, with raster flags, assembly and virtual-shadow metadata.'),
('lab-pixel-write','Engine/Shaders/Private/Nanite/NaniteWritePixel.ush',19,92,'The supported 64-bit visibility path packs identity/depth and performs an atomic maximum.'),
('lab-barycentrics','Engine/Shaders/Private/Barycentrics.ush',60,101,'Triangle reconstruction computes perspective-correct barycentric values and quotient-rule derivatives.'),
('lab-tessellation-factors','Engine/Shaders/Private/Nanite/NaniteTessellation.ush',61,143,'Tessellation factors use view-space edge lengths/depth, LOD scale and dicing controls before pattern initialization.'),
('lab-tessellated-patch','Engine/Shaders/Private/Nanite/NaniteTessellation.ush',135,211,'FTessellatedPatch stores a table offset and packed pattern/output counts and decodes barycentric vertices/connectivity.')
]

if __name__=='__main__':
    records=[]
    for id,path,line,end,claim in RECORDS:
        text='\n'.join((ENGINE/path).read_text(encoding='utf-8-sig').splitlines()[line-1:end])
        records.append(dict(id=id,path=path,line=line,end_line=end,claim=claim,sha256=hashlib.sha256(text.encode()).hexdigest(),repository='unreal',commit=REVISION))
    target=HERE.parents[1]/'evidence/nanite-lab.json'
    target.write_text(json.dumps(records,indent=2)+'\n',encoding='utf-8')
    print(f'{len(records)} evidence records -> {target}')
