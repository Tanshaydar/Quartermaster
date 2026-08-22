"""
VaultMCP project auditor: sniff the active game project so agents (and the UI)
can warn about engine/pipeline incompatibilities BEFORE importing anything.

Detected:
  Unity : ProjectSettings/ProjectVersion.txt          -> editor version
          Packages/manifest.json                      -> HDRP / URP package
          ProjectSettings/GraphicsSettings.asset      -> custom pipeline fallback
  Unreal: <Name>.uproject "EngineAssociation"         -> UE 5.x
          Config/DefaultEngine.ini                    -> renderer hints

Used by:
  - MCP tool audit_project(project_dir)
  - import_asset_to_project() pre-import compatibility warning
"""
import glob
import json
import os
import re
from typing import Any, Dict, List


def audit_unity(project_dir: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {"engine": "unity", "pipeline": "Built-in", "version": None}

    pv = os.path.join(project_dir, "ProjectSettings", "ProjectVersion.txt")
    if os.path.isfile(pv):
        with open(pv, "r", encoding="utf-8", errors="ignore") as f:
            m = re.search(r"m_EditorVersion:\s*(\S+)", f.read())
        if m:
            v = m.group(1)
            major = v.split(".")[0]
            pretty = "Unity " + ("6" if major.startswith("6") or major.startswith("20") else v.rsplit(".", 1)[0])
            info["version"] = f"{pretty} ({v})"

    manifest = os.path.join(project_dir, "Packages", "manifest.json")
    deps: Dict[str, Any] = {}
    if os.path.isfile(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                deps = json.load(f).get("dependencies", {})
        except Exception:
            pass

    has_hdrp = any(k.startswith("com.unity.render-pipelines.high-definition") for k in deps)
    has_urp = any(k.startswith("com.unity.render-pipelines.universal") for k in deps)

    if has_hdrp and has_urp:
        info["pipeline"] = "HDRP+URP"
    elif has_hdrp:
        info["pipeline"] = "HDRP"
    elif has_urp:
        info["pipeline"] = "URP"
    else:
        # fall back to GraphicsSettings: a custom SRP asset is referenced there
        gs = os.path.join(project_dir, "ProjectSettings", "GraphicsSettings.asset")
        if os.path.isfile(gs):
            with open(gs, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            if re.search(r"m_CustomRenderPipeline:\s*\{fileID:\s*4800000", txt):
                info["pipeline"] = "SRP (unresolved)"
        # else stays Built-in

    info["packages"] = sorted(deps.keys())
    return info


def audit_unreal(project_dir: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {"engine": "unreal", "pipeline": None, "version": None}

    uprojects = glob.glob(os.path.join(project_dir, "*.uproject"))
    if uprojects:
        try:
            with open(uprojects[0], "r", encoding="utf-8") as f:
                assoc = json.load(f).get("EngineAssociation")
            if assoc:
                info["version"] = f"Unreal Engine {assoc}"
        except Exception:
            pass

    ini = os.path.join(project_dir, "Config", "DefaultEngine.ini")
    if os.path.isfile(ini):
        with open(ini, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if re.search(r"GlobalDefaultGameMode", txt) or True:
            hints = []
            if re.search(r"^r\.DynamicGlobalIlluminationMethod=(\d)", txt, re.M):
                hints.append("Lumen-era project")
            if re.search(r"SupportForwardShading", txt):
                hints.append("forward shading")
            if hints:
                info["pipeline"] = "; ".join(hints)
    return info


def audit_project(project_dir: str) -> Dict[str, Any]:
    """Detect engine, version and render pipeline of a game project."""
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise ValueError(f"Not a directory: {project_dir}")

    if os.path.isdir(os.path.join(project_dir, "Assets")) and \
            (os.path.isdir(os.path.join(project_dir, "ProjectSettings")) or
             os.path.isfile(os.path.join(project_dir, "ProjectSettings", "ProjectVersion.txt"))):
        result = audit_unity(project_dir)
    elif glob.glob(os.path.join(project_dir, "*.uproject")) or \
            os.path.isdir(os.path.join(project_dir, "Config")) and \
            os.path.isdir(os.path.join(project_dir, "Content")):
        result = audit_unreal(project_dir)
    else:
        raise ValueError(f"'{project_dir}' does not look like a Unity or Unreal project "
                         "(no Assets/ProjectSettings or .uproject/Content found)")

    result["project_dir"] = project_dir
    return result


def compatibility_warning(asset: Dict[str, Any], project_info: Dict[str, Any]) -> List[str]:
    """Human-readable warnings when an asset may not fit the audited project."""
    warnings: List[str] = []
    if not asset or not project_info:
        return warnings

    if asset.get("source") == "fab" and project_info.get("engine") == "unity":
        warnings.append(
            f"This is a Fab/Unreal listing but the target project is Unity "
            f"({project_info.get('version')}). Expect full re-authoring: meshes "
            f"(FBX ok), materials must be rebuilt, Nanite-specific LODs are useless.")
    elif asset.get("source") == "unity" and project_info.get("engine") == "unreal":
        warnings.append("This is a Unity Asset Store package; the target project is Unreal.")

    pipes = asset.get("render_pipelines") or []
    proj_pipe = project_info.get("pipeline")
    if project_info.get("engine") == "unity" and pipes and proj_pipe:
        proj_set = set(p.strip().upper() for p in proj_pipe.replace("HDRP+URP", "HDRP,URP").split(","))
        asset_set = set(p.upper() for p in pipes)
        if proj_set & {"HDRP", "URP", "BUILT-IN"} and not (asset_set & proj_set):
            warnings.append(
                f"Pipeline mismatch: asset supports {'/'.join(sorted(asset_set))} but project uses {proj_pipe}.")
    return warnings


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m src.project_audit <project_dir>")
    else:
        print(json.dumps(audit_project(sys.argv[1]), indent=2))
