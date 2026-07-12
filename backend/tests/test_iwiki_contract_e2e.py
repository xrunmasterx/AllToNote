from collections.abc import Mapping
import os
from pathlib import Path
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.iwiki_client import IWikiClient


KNOWN_INDEX_STATES = {"missing", "building", "failed", "stale", "ready"}
REQUIRED_CAPABILITIES = {"inspect", "validate", "query_native", "qmd_index"}
REQUIRED_QUERY_ITEM_FIELDS = {
    "path",
    "scope",
    "score",
    "snippet",
    "title",
    "updated_at",
}


def test_real_iwiki_cli_contract():
    workspace_value = os.environ.get("IWIKI_TEST_WORKSPACE")
    if not workspace_value:
        pytest.skip("set IWIKI_TEST_WORKSPACE for cross-repository contract test")

    client = IWikiClient.discover()
    workspace = Path(workspace_value)

    inspected = client.inspect(workspace)
    assert inspected.schema_version == 2
    assert inspected.cli_protocol_version == 1
    assert REQUIRED_CAPABILITIES <= inspected.capabilities

    validation = client.validate(workspace)
    assert validation["valid"] is True
    assert isinstance(validation["issues"], list)

    result = client.query(workspace, scope="common", text="RHI", limit=3)
    assert result["query"] == "RHI"
    assert result["scope"] == "common"
    assert result["index_backend"] == "native"
    assert isinstance(result["items"], list)
    assert 0 <= len(result["items"]) <= 3
    for item in result["items"]:
        assert isinstance(item, Mapping)
        assert REQUIRED_QUERY_ITEM_FIELDS <= item.keys()
        assert item["scope"] == "common"
        assert isinstance(item["path"], str)
        assert item["path"].startswith("wiki/common/")

    index = client.index_status(workspace)
    assert index["state"] in KNOWN_INDEX_STATES
    assert index["backend"] == "qmd"
    assert isinstance(index["database_path"], str)
