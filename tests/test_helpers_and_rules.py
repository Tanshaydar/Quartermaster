import pytest
from src.local_scan import _norm
from src.store_client import classify_asset
from src.mcp_server import get_stack_recommendations
from src.stack_rules import validate_stack, list_recipes
import json


def test_norm():
    assert _norm("Polygon - Fantasy Kingdom (v1.2)") == "polygon fantasy kingdom"
    assert _norm("Mega_Pack_Sci-Fi") == "mega pack sci fi"


def test_classify_asset():
    cls = classify_asset("Stylized Medieval Castle Environment Pack (URP / HDRP)")
    assert cls["category"] in ("3D Environments", "3D Models & Props", "General")
    assert any("URP" in p or "HDRP" in p for p in cls["render_pipelines"])


def test_stack_rules():
    recipes = list_recipes()
    assert len(recipes) > 0
    assert "name" in recipes[0]
    assert "slots" in recipes[0]

    val = validate_stack(["unity_123", "unity_456"])
    assert "status" in val
