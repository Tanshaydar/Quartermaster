import os
import pytest
import tempfile
from src.unpacker import _sanitize_package_path, _safe_target, _strip_reason


def test_sanitize_path_traversal():
    rel, parts = _sanitize_package_path("../../../etc/passwd")
    assert ".." not in parts
    assert "passwd" in parts


def test_sanitize_drive_letters():
    rel, parts = _sanitize_package_path("C:/Assets/Textures/Rock_01.png")
    assert parts == ["Textures", "Rock_01.png"]

    rel2, parts2 = _sanitize_package_path("Assets/Audio/Track:Reverb.wav")
    assert parts2 == ["Audio", "Track:Reverb.wav"]


def test_sanitize_null_bytes_and_control_chars():
    rel, parts = _sanitize_package_path("Assets/Models/Hero\x00\x01\x1fCharacter.fbx")
    assert "\x00" not in rel
    assert "\x01" not in rel
    assert parts == ["Models", "HeroCharacter.fbx"]


def test_safe_target_enforcement():
    with tempfile.TemporaryDirectory() as tmp_proj:
        target = _safe_target(tmp_proj, ["Models", "Sword.fbx"])
        assert target.startswith(os.path.abspath(tmp_proj))
        assert target.endswith(os.path.join("Assets", "Models", "Sword.fbx"))

        target_non_assets = _safe_target(tmp_proj, ["ThirdParty", "Lib.dll"], original_rel="Packages/Lib.dll")
        assert target_non_assets.startswith(os.path.abspath(tmp_proj))
        assert "Assets" in target_non_assets


def test_strip_demo_rules():
    strip_dirs = {"demo", "demos", "samples", "sample", "example", "examples", "test"}
    strip_exts = {".unity", ".mp4", ".mov", ".avi"}

    assert _strip_reason("Assets/Hero/Demo/Scene.unity", strip_dirs, strip_exts) is not None
    assert _strip_reason("Assets/Hero/Textures/Trailer.mp4", strip_dirs, strip_exts) is not None
    assert _strip_reason("Assets/Hero/Models/Hero.fbx", strip_dirs, strip_exts) is None
