"""Local processor contract without loading weights or making model calls."""

import subprocess
import threading
from contextlib import contextmanager

import pytest

from frameseek import local_vision
from frameseek.knowledge_local import LocalKnowledgeProcessor, model_fingerprint
from frameseek.local_routes import _SLOT


def test_local_processor_reuses_shared_worker_and_detects_weight_change(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    weights = model / "model.safetensors"
    weights.write_bytes(b"synthetic weights")
    config = (tmp_path / "python", model)
    calls = []

    class Worker:
        identity = config
        process = type("Process", (), {"poll": lambda self: None})()

        def infer(self, *args, **kwargs):
            calls.append(1)
            return {"text": "{}"}

    monkeypatch.setattr(local_vision, "_CLIENT", Worker())
    processor = LocalKnowledgeProcessor(config, tmp_path / "worker.log")
    local_vision._CLIENT.knowledge_signature = processor.signature
    with processor.call_scope():
        assert processor.infer("synthetic") == {"text": "{}"}
    assert calls == [1]
    old = model_fingerprint(model)
    weights.write_bytes(b"changed weights")
    assert model_fingerprint(model) != old
    with pytest.raises(ValueError, match="local_knowledge_model_changed"):
        with processor.call_scope():
            pass


def test_processor_identity_and_live_guard_include_tokenizer_and_preprocessing(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    tokenizer = model / "tokenizer.json"
    tokenizer.write_text('{"version":1}')
    preprocessor = model / "preprocessor_config.json"
    preprocessor.write_text('{"size":1}')
    processor = LocalKnowledgeProcessor((tmp_path / "python", model), tmp_path / "log")
    original = model_fingerprint(model)
    tokenizer.write_text('{"version":2}')
    assert model_fingerprint(model) != original
    with pytest.raises(ValueError, match="local_knowledge_model_changed"):
        with processor.call_scope():
            pass
    original = model_fingerprint(model)
    preprocessor.write_text('{"size":2}')
    assert model_fingerprint(model) != original


def test_fingerprint_rejects_files_added_while_hashing(tmp_path, monkeypatch):
    from frameseek import knowledge_local

    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    original = knowledge_local.model_files
    calls = []

    def changed(directory):
        calls.append(1)
        if len(calls) == 2:
            (model / "tokenizer.json").write_text("{}")
        return original(directory)

    monkeypatch.setattr(knowledge_local, "model_files", changed)
    with pytest.raises(ValueError, match="local_knowledge_model_changed"):
        model_fingerprint(model)


def test_new_jobs_use_new_processor_identity_after_model_change(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from frameseek.knowledge_jobs import FixtureKnowledgeJobs

    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    tokenizer = model / "tokenizer.json"
    tokenizer.write_text('{"version":1}')
    config = (tmp_path / "python", model)
    monkeypatch.setenv("FS_KNOWLEDGE_LOCAL", "1")
    monkeypatch.setattr(local_vision, "configured", lambda: config)
    jobs = FixtureKnowledgeJobs(
        SimpleNamespace(root=tmp_path),
        SimpleNamespace(state=SimpleNamespace()),
        SimpleNamespace(mode="fixture"),
    )
    first = jobs.processor()
    assert jobs.processor() is first
    tokenizer.write_text('{"version":2}')
    second = jobs.processor()
    assert second is not first and second.revision != first.revision


def test_unattributed_worker_is_replaced_before_new_revision_runs(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    config = (tmp_path / "python", model)
    stopped = []

    class Process:
        def poll(self):
            return None if not stopped else 0

        def terminate(self):
            stopped.append(True)

        def wait(self, timeout):
            assert timeout == 5

    old = type("Old", (), {"identity": config, "process": Process()})()
    created = []

    class New:
        def __init__(self, *args):
            self.identity = config
            self.process = type("P", (), {"poll": lambda self: None})()
            created.append(self)

    monkeypatch.setattr(local_vision, "_CLIENT", old)
    monkeypatch.setattr(local_vision, "LocalClient", New)
    processor = LocalKnowledgeProcessor(config, tmp_path / "log")
    with processor.call_scope():
        assert local_vision._CLIENT is created[0]
        assert created[0].knowledge_signature == processor.signature
    assert stopped == [True]


def test_unattributed_worker_that_does_not_stop_is_not_replaced(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    config = (tmp_path / "python", model)

    class Process:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("local-worker", timeout)

    old = type("Old", (), {"identity": config, "process": Process()})()
    monkeypatch.setattr(local_vision, "_CLIENT", old)
    processor = LocalKnowledgeProcessor(config, tmp_path / "log")
    with pytest.raises(ValueError, match="local_model_worker_stop_timeout"):
        with processor.call_scope():
            pass
    assert local_vision._CLIENT is old
    assert old.cancelled


def test_change_while_worker_starts_cannot_use_stale_revision(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    config = (tmp_path / "python", model)
    stopped = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            stopped.append(True)

    class New:
        def __init__(self, *args):
            self.identity = config
            self.process = Process()
            (model / "tokenizer.json").write_text("{}")

    monkeypatch.setattr(local_vision, "_CLIENT", None)
    monkeypatch.setattr(local_vision, "LocalClient", New)
    processor = LocalKnowledgeProcessor(config, tmp_path / "log")
    with pytest.raises(ValueError, match="local_knowledge_model_changed"):
        with processor.call_scope():
            pass
    assert stopped == [True]
    assert local_vision._CLIENT is None


def test_cancel_while_waiting_for_local_slot_does_not_start_worker(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"weights")
    processor = LocalKnowledgeProcessor((tmp_path / "python", model), tmp_path / "log")
    active = threading.Event()
    active.set()
    processor.active = active.is_set
    errors = []
    assert _SLOT.acquire(blocking=False)
    try:

        def worker():
            try:
                with processor.call_scope():
                    raise AssertionError("cancelled task acquired the model")
            except ValueError as error:
                errors.append(str(error))

        thread = threading.Thread(target=worker)
        thread.start()
        active.clear()
        thread.join(timeout=2)
        assert not thread.is_alive() and errors == ["knowledge_cancelled"]
    finally:
        _SLOT.release()


def test_cancel_only_interrupts_the_matching_local_dispatch(monkeypatch):
    from frameseek.knowledge_jobs import FixtureKnowledgeJobs

    jobs = FixtureKnowledgeJobs(None, None, None)
    entered, release = threading.Event(), threading.Event()
    stopped = []
    monkeypatch.setattr(local_vision, "cancel_worker", lambda: stopped.append(True))

    class Local:
        local_only = True

        @contextmanager
        def call_scope(self, active, on_acquired):
            on_acquired()
            entered.set()
            assert release.wait(2)
            yield

    def work():
        with jobs.call_scope(
            Local(), lambda: True, interactive=True, token=("supplement", "a", "q")
        ):
            pass

    thread = threading.Thread(target=work)
    thread.start()
    assert entered.wait(2)
    jobs.interrupt_dispatch(("supplement", "other", "q"))
    assert not stopped
    jobs.interrupt_dispatch(("supplement", "a", "q"))
    assert stopped == [True]
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
