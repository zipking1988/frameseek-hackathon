"""Contracts for a reusable, sampled video knowledge index.

This module has no provider or worker side effects. A caller must authorize and
account for extraction before passing validated observations to it.
"""

import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, model_serializer, model_validator

from frameseek.occurrences import CatalogFrame, Observation, ObservationIndex, Subject, fingerprint
from frameseek.schema import Contract, FiniteSeconds, Identifier


class KnowledgeSegment(Contract):
    ordinal: int = Field(ge=0, le=449)
    start: FiniteSeconds
    end: FiniteSeconds
    frames: list[CatalogFrame] = Field(min_length=1, max_length=8)
    observations: list[Observation] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_segment(self):
        if not self.start < self.end <= self.start + 8:
            raise ValueError("invalid_knowledge_window")
        ids = [frame.frame_id for frame in self.frames]
        observed = [observation.frame_id for observation in self.observations]
        if len(set(ids)) != len(ids) or len(set(observed)) != len(observed):
            raise ValueError("duplicate_knowledge_frame")
        if set(ids) != set(observed):
            raise ValueError("incomplete_knowledge_segment")
        if any(
            not self.start <= frame.t < self.end or frame.end is not None for frame in self.frames
        ):
            raise ValueError("invalid_knowledge_frame_time")
        times = [frame.t for frame in self.frames]
        if any(b - a < 0.99999 for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("knowledge_sampling_rate_exceeded")
        if any(
            subject.track_id or subject.entity_id or subject.event_keys
            for observation in self.observations
            for subject in observation.subjects
        ):
            raise ValueError("knowledge_identity_not_supported")
        return self


class KnowledgeSupplement(Contract):
    """Additional observations for existing frames, never cross-frame identities."""

    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    processor_revision: str = Field(min_length=1, max_length=200)
    window_ordinals: list[int] = Field(min_length=1, max_length=2)
    observations: list[Observation] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_supplement(self):
        if len(set(self.window_ordinals)) != len(self.window_ordinals) or any(
            not 0 <= ordinal <= 449 for ordinal in self.window_ordinals
        ):
            raise ValueError("invalid_supplement_windows")
        ids = [observation.frame_id for observation in self.observations]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_supplement_frame")
        if any(
            observation.inventory_complete or observation.event_inventory_complete
            for observation in self.observations
        ):
            raise ValueError("supplement_cannot_claim_complete_inventory")
        if any(
            subject.track_id or subject.entity_id or subject.event_keys
            for observation in self.observations
            for subject in observation.subjects
        ):
            raise ValueError("knowledge_identity_not_supported")
        return self


class VideoKnowledge(Contract):
    format: Literal["frameseek_video_knowledge_v1"] = "frameseek_video_knowledge_v1"
    video: Identifier
    generation: int = Field(ge=1)
    duration: float = Field(gt=0, le=3600, allow_inf_nan=False)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    processor_revision: str = Field(min_length=1, max_length=200)
    extraction_config: str = Field(min_length=1, max_length=200)
    sampling: Literal["up_to_8_frames_per_8_seconds"] = "up_to_8_frames_per_8_seconds"
    scene_boundaries: list[FiniteSeconds] = Field(default_factory=list, max_length=10000)
    segments: list[KnowledgeSegment] = Field(default_factory=list, max_length=450)
    supplements: list[KnowledgeSupplement] = Field(default_factory=list, max_length=100)
    unavailable_capabilities: list[str] = Field(default_factory=lambda: ["speech_transcription"])

    @model_serializer(mode="wrap")
    def preserve_base_version(self, handler):
        value = handler(self)
        if not self.supplements:
            value.pop("supplements", None)
        return value

    @model_validator(mode="after")
    def validate_plan(self):
        if any(not 0 < cut < self.duration for cut in self.scene_boundaries) or any(
            a >= b for a, b in zip(self.scene_boundaries, self.scene_boundaries[1:], strict=False)
        ):
            raise ValueError("invalid_knowledge_scene_boundaries")
        count = math.ceil(self.duration / 8)
        seen = set()
        for segment in self.segments:
            if segment.ordinal in seen or segment.ordinal >= count:
                raise ValueError("invalid_knowledge_segment_ordinal")
            if segment.start != segment.ordinal * 8 or segment.end != min(
                (segment.ordinal + 1) * 8, self.duration
            ):
                raise ValueError("knowledge_window_plan_mismatch")
            seen.add(segment.ordinal)
        frames = {frame.frame_id: frame for segment in self.segments for frame in segment.frames}
        requests = set()
        for supplement in self.supplements:
            if supplement.request_fingerprint in requests:
                raise ValueError("duplicate_supplement_request")
            requests.add(supplement.request_fingerprint)
            if any(
                observation.frame_id not in frames
                or math.floor(frames[observation.frame_id].t / 8) not in supplement.window_ordinals
                for observation in supplement.observations
            ):
                raise ValueError("foreign_supplement_frame")
        # ObservationIndex validates source-local frame IDs, actual URIs, ordering,
        # unique frame times/ordinals and per-frame subject binding.
        if self.segments:
            self.observation_index()
        return self

    def observation_index(self):
        frames = sorted(
            (frame for segment in self.segments for frame in segment.frames),
            key=lambda frame: (frame.t, frame.ordinal),
        )
        observations = {
            ob.frame_id: ob.model_copy(deep=True)
            for segment in self.segments
            for ob in segment.observations
        }
        for supplement in self.supplements:
            for row in supplement.observations:
                target = observations[row.frame_id]
                for number, subject in enumerate(row.subjects):
                    data = subject.model_dump(mode="json")
                    # Supplemental entities remain independent; no attribute is
                    # joined to an earlier subject merely because IDs resemble it.
                    data["id"] = f"supp_{supplement.request_fingerprint[:20]}_{number}"
                    target.subjects.append(Subject.model_validate(data))
                observations[row.frame_id] = Observation.model_validate(target.model_dump())
        return ObservationIndex(
            video=self.video,
            generation=self.generation,
            duration=self.duration,
            source_fingerprint=self.source_fingerprint,
            extractor_revision=self.processor_revision,
            catalog_complete=False,
            frames=frames,
            observations=list(observations.values()),
        )

    def cache_key(self):
        return fingerprint(
            {
                "video": self.video,
                "generation": self.generation,
                "source_fingerprint": self.source_fingerprint,
                "processor_revision": self.processor_revision,
                "extraction_config": self.extraction_config,
                "duration": self.duration,
                "sampling": self.sampling,
                "scene_boundaries": self.scene_boundaries,
            }
        )

    def pending(self):
        completed = {segment.ordinal for segment in self.segments}
        return [i for i in range(math.ceil(self.duration / 8)) if i not in completed]

    def checkpoint(self, segment):
        segment = KnowledgeSegment.model_validate(segment)
        old = next((item for item in self.segments if item.ordinal == segment.ordinal), None)
        if old is not None:
            if old != segment:
                raise ValueError("knowledge_checkpoint_conflict")
            return self
        data = self.model_dump(mode="json")
        data["segments"].append(segment.model_dump(mode="json"))
        data["segments"].sort(key=lambda item: item["ordinal"])
        return VideoKnowledge.model_validate(data)

    def publishable_index(self):
        if self.pending():
            raise ValueError("knowledge_index_incomplete")
        return self.observation_index()

    def supplement(self, value, expected_version):
        item = KnowledgeSupplement.model_validate(value)
        if expected_version != fingerprint(self.model_dump(mode="json")):
            raise ValueError("knowledge_revision_conflict")
        if item.source_version != expected_version:
            raise ValueError("stale_supplement_version")
        if self.pending():
            raise ValueError("knowledge_index_incomplete")
        if any(
            existing.request_fingerprint == item.request_fingerprint
            for existing in self.supplements
        ):
            raise ValueError("supplement_already_recorded")
        data = self.model_dump(mode="json")
        data.setdefault("supplements", []).append(item.model_dump(mode="json"))
        return VideoKnowledge.model_validate(data)


MAX_KNOWLEDGE_BYTES = 64 * 1024 * 1024


def validate_global_sampling(index):
    """Validate the extraction limit across window boundaries as well."""
    times = [frame.t for frame in index.frames]
    if any(b - a < 0.99999 for a, b in zip(times, times[1:], strict=False)):
        raise ValueError("knowledge_sampling_rate_exceeded")


class KnowledgeStore:
    """Private generation-specific checkpoints and immutable published versions.

    The owner and expiry must be checked by the caller before using this store.
    Its lock/CAS protects checkpoint writers; it does not authorize a provider call.
    """

    def __init__(self, directory: Path, identity: VideoKnowledge):
        self.directory = Path(directory)
        self.identity = identity
        self.key = identity.cache_key()
        self.base = self.directory / f"knowledge-g{identity.generation}-{self.key}"
        self.base.mkdir(mode=0o700, exist_ok=True)
        if self.base.is_symlink() or not self.base.is_dir():
            raise ValueError("invalid_knowledge_directory")

    @contextmanager
    def locked(self):
        fd = os.open(self.base / "store.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_KNOWLEDGE_BYTES:
                raise ValueError("knowledge_size_limit")
            return stream.read(MAX_KNOWLEDGE_BYTES + 1)

    def _write(self, path, data):
        if len(data) > MAX_KNOWLEDGE_BYTES:
            raise ValueError("knowledge_size_limit")
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=self.base)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory_fd = os.open(self.base, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _load(self):
        path = self.base / "checkpoint.json"
        if not path.exists() and not path.is_symlink():
            return self.identity
        state = VideoKnowledge.model_validate_json(self._read(path))
        if state.cache_key() != self.key:
            raise ValueError("stale_knowledge_checkpoint")
        return state

    def load(self):
        with self.locked():
            return self._load()

    def checkpoint(self, segment, expected_snapshot):
        with self.locked():
            state = self._load()
            if fingerprint(state.model_dump(mode="json")) != expected_snapshot:
                raise ValueError("knowledge_revision_conflict")
            updated = state.checkpoint(segment)
            self._write(self.base / "checkpoint.json", updated.model_dump_json().encode())
            return updated

    def supplement(self, item, expected_snapshot):
        with self.locked():
            state = self._load()
            updated = state.supplement(item, expected_snapshot)
            self._write(self.base / "checkpoint.json", updated.model_dump_json().encode())
            return updated

    def stage_supplement(self, item, source_version):
        """Write an immutable candidate without changing the active checkpoint.

        The caller must separately publish its version in an authorized DB CAS.
        A cancellation or failed CAS leaves only an unreachable candidate.
        """
        from frameseek.index_service import source_digest

        state = self.read_version(source_version)
        updated = state.supplement(item, source_version)
        index = updated.publishable_index()
        if source_digest(self.directory) != updated.source_fingerprint:
            raise ValueError("knowledge_source_changed")
        frame_directory = self.directory / "frames"
        for frame in index.frames:
            path = frame_directory / frame.file
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve().parent != frame_directory.resolve()
            ):
                raise ValueError("knowledge_evidence_missing")
        encoded = updated.model_dump_json().encode()
        version = fingerprint(updated.model_dump(mode="json"))
        version_path = self.base / f"version-{version}.json"
        with self.locked():
            if version_path.exists() or version_path.is_symlink():
                if self._read(version_path) != encoded:
                    raise ValueError("knowledge_version_conflict")
            else:
                self._write(version_path, encoded)
        return version

    def publish(self, expected_snapshot):
        from frameseek.index_service import source_digest

        with self.locked():
            state = self._load()
            if fingerprint(state.model_dump(mode="json")) != expected_snapshot:
                raise ValueError("knowledge_revision_conflict")
            index = state.publishable_index()
            validate_global_sampling(index)
            if source_digest(self.directory) != state.source_fingerprint:
                raise ValueError("knowledge_source_changed")
            frame_directory = self.directory / "frames"
            for frame in index.frames:
                path = frame_directory / frame.file
                if path.is_symlink() or not path.is_file():
                    raise ValueError("knowledge_evidence_missing")
            encoded = state.model_dump_json().encode()
            version = fingerprint(state.model_dump(mode="json"))
            version_path = self.base / f"version-{version}.json"
            if version_path.exists() or version_path.is_symlink():
                if self._read(version_path) != encoded:
                    raise ValueError("knowledge_version_conflict")
            else:
                self._write(version_path, encoded)
            self._write(self.base / "active.json", json.dumps({"version": version}).encode())
            return version

    def read_version(self, version):
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError("invalid_knowledge_version")
        with self.locked():
            state = VideoKnowledge.model_validate_json(
                self._read(self.base / f"version-{version}.json")
            )
            snapshot = fingerprint(state.model_dump(mode="json"))
            if state.cache_key() != self.key or snapshot != version:
                raise ValueError("invalid_knowledge_version")
            state.publishable_index()
            return state


def load_published_state(directory, video, generation, cache_key, version, source_hash):
    """Read a named published revision without creating missing private paths."""
    for key in (cache_key, version):
        if (
            not isinstance(key, str)
            or len(key) != 64
            or any(c not in "0123456789abcdef" for c in key)
        ):
            raise ValueError("invalid_knowledge_version")
    base = Path(directory) / f"knowledge-g{generation}-{cache_key}"
    if base.is_symlink() or not base.is_dir():
        raise ValueError("invalid_knowledge_directory")
    fd = os.open(base / f"version-{version}.json", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_KNOWLEDGE_BYTES:
            raise ValueError("knowledge_size_limit")
        state = VideoKnowledge.model_validate_json(stream.read(MAX_KNOWLEDGE_BYTES + 1))
    if (
        state.video != video
        or state.generation != generation
        or state.source_fingerprint != source_hash
        or state.cache_key() != cache_key
        or fingerprint(state.model_dump(mode="json")) != version
    ):
        raise ValueError("stale_knowledge_version")
    state.publishable_index()
    return state


def load_published_knowledge(directory, video, generation, cache_key, version, source_hash):
    return load_published_state(
        directory, video, generation, cache_key, version, source_hash
    ).publishable_index()
