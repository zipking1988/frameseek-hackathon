"""Finite configuration. Environment > TOML > defaults; no spending approvals here."""

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    mode: Literal["fixture", "real"] = "fixture"
    scene_threshold: float = Field(default=0.3, ge=0, le=1)
    coarse_frames: int = Field(default=12, ge=8, le=16)
    candidate_k: int = Field(default=10, ge=1, le=100)
    rerank_k: int = Field(default=5, ge=1, le=100)
    ttl_hours: int = Field(default=24, ge=1, le=168)
    max_window_seconds: Literal[120] = 120
    overlap_seconds: Literal[5] = 5
    max_fps: Literal[1] = 1
    max_frames: Literal[128] = 128
    primary_cap: Literal[30] = 30
    coarse_retry_cap: Literal[0] = 0
    zoom_cap: Literal[3] = 3
    text_repair_cap: Literal[1] = 1

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_limits(cls, values):
        if isinstance(values, dict) and any(
            isinstance(v, bool) for k, v in values.items() if k != "mode"
        ):
            raise ValueError("Boolean values are not numeric limits")
        return values

    @model_validator(mode="after")
    def check_rerank(self) -> "Settings":
        if self.rerank_k > self.candidate_k:
            raise ValueError("rerank_k must not exceed candidate_k")
        return self


class ConfigurationError(ValueError):
    """Safe configuration error: never include supplied values or secrets."""


def load_settings(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    values: dict = {}
    if path is None and env.get("FRAMESEEK_CONFIG"):
        path = Path(env["FRAMESEEK_CONFIG"])
    if path is not None:
        try:
            document = tomllib.loads(path.read_text())
            if set(document) != {"frameseek"} or not isinstance(document["frameseek"], dict):
                raise ConfigurationError("Expected one [frameseek] configuration table")
            values = document["frameseek"].copy()
        except (OSError, tomllib.TOMLDecodeError):
            raise ConfigurationError("Configuration file could not be read as TOML") from None
    fields = set(Settings.model_fields)
    for key, value in env.items():
        if not key.startswith("FRAMESEEK_") or key == "FRAMESEEK_CONFIG":
            continue
        name = key.removeprefix("FRAMESEEK_").lower()
        if name not in fields:
            raise ConfigurationError("Unsupported FRAMESEEK configuration variable")
        try:
            values[name] = json.loads(value)
        except json.JSONDecodeError:
            values[name] = value
    try:
        return Settings.model_validate(values)
    except ValidationError:
        raise ConfigurationError("Configuration violates a field or protected limit") from None
