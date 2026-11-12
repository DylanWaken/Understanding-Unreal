"""Read back selected real Lumen GPU atlas textures using UE's DumpGPU tool.
The dump is local-only; upload and file-explorer launch are disabled.
"""
import unreal
import os
from pathlib import Path
ROOT=Path(os.environ.get('ARDA_UNREAL_LAB',str(Path.home()/'AppData/Local/Temp/arda-unreal-lab')))
LEVEL=unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
LEVEL.load_level('/Game/Labs/LumenRoom')
WORLD=unreal.EditorLevelLibrary.get_editor_world()
camera=next(a for a in ACTORS.get_all_level_actors() if isinstance(a,unreal.CameraActor))
LEVEL.pilot_level_actor(camera)
LEVEL.editor_set_game_view(True)
unreal.ArdaLabCapture.set_viewport_mode('lit','')
def cmd(c):
    unreal.log('ARDA_GPU_COMMAND '+c)
    unreal.SystemLibrary.execute_console_command(WORLD,c)
for c in ['r.Lumen.HardwareRayTracing 0','r.LumenScene.SurfaceCache.Compress 0','r.DumpGPU.Explore 0','r.DumpGPU.Upload 0','r.DumpGPU.Buffer 0','r.DumpGPU.MaxStagingSize 512','r.DumpGPU.Test.PrettifyResourceFileNames 1']:
    cmd(c)
state={'frame':0,'busy':False}
def tick(dt):
    if state['busy']:return
    state['busy']=True
    try:
        state['frame']+=1
        unreal.ArdaLabCapture.render_viewport()
        if state['frame']==300:
            unreal.ArdaLabCapture.capture_viewport(str(ROOT/'captures/lumen-native-temporal.png'))
        if state['frame']==400:
            cmd('DumpGPU -path='+str(ROOT/'gpu-dump-final').replace('\\','/')+' -unified_filter="*@Lumen.SceneAlbedo|*@Lumen.SceneNormal|*@Lumen.SceneDepth|*@Lumen.SceneFinalLighting|*@*RadianceCache*FinalRadianceAtlas|*@*ScreenProbe*Radiance"')
        if state['frame']>800:
            unreal.unregister_slate_post_tick_callback(HANDLE)
            unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    finally:
        state['busy']=False
unreal.EditorPythonScripting.set_keep_python_script_alive(True)
HANDLE=unreal.register_slate_post_tick_callback(tick)
