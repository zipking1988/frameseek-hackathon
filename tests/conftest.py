"""Never let a test reset, delete or inspect the running preview's private workspace."""

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_DATA_DIR", str(tmp_path / "isolated_workspace"))
    monkeypatch.delenv("FS_RUNTIME_FILE", raising=False)
