"""Opt-in local-only knowledge processor. No network or paid model calls."""

import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path

from frameseek import local_vision
from frameseek.local_routes import _SLOT


def model_files(directory):
    directory = Path(directory)
    weights = list(directory.glob("*.safetensors"))
    files = sorted(
        {
            path
            for pattern in ("*.json", "*.safetensors", "*.model", "*.txt", "*.jinja")
            for path in directory.glob(pattern)
        }
    )
    if (
        not (directory / "config.json").is_file()
        or not weights
        or len(files) > 100
        or any(not path.is_file() for path in files)
    ):
        raise ValueError("local_knowledge_model_invalid")
    return files


def model_signature(directory):
    files = model_files(directory)
    return tuple((path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in files)


def fingerprint_with_signature(directory):
    directory = Path(directory)
    signature = model_signature(directory)
    digest = hashlib.sha256()
    for name, _, _ in signature:
        path = directory / name
        digest.update(path.name.encode())
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    if model_signature(directory) != signature:
        raise ValueError("local_knowledge_model_changed")
    return digest.hexdigest(), signature


def model_fingerprint(directory):
    return fingerprint_with_signature(directory)[0]


class LocalKnowledgeProcessor:
    fixture_only = False
    local_only = True
    # Supplement receipt identities retain the existing processor configuration.
    extraction_config = "knowledge-v3-8sec-8frames-4096tokens"

    def __init__(self, configuration, log_path):
        self.configuration = configuration
        self.log_path = Path(log_path)
        digest, self.signature = fingerprint_with_signature(configuration[1])
        self.revision = "local-" + digest
        self.active = lambda: True

    @contextmanager
    def call_scope(self, active=None, on_acquired=None):
        active = active or self.active
        while active():
            if _SLOT.acquire(timeout=0.2):
                break
        else:
            raise ValueError("knowledge_cancelled")
        try:
            if on_acquired:
                on_acquired()
            with local_vision._LOCK:
                if not active():
                    raise ValueError("knowledge_cancelled")
                if model_signature(self.configuration[1]) != self.signature:
                    raise ValueError("local_knowledge_model_changed")
                client = local_vision._CLIENT
                # A worker loaded before the current file signature cannot be
                # attributed to this revision, even when its paths are equal.
                if (
                    client is not None
                    and client.process.poll() is None
                    and (
                        client.identity != self.configuration
                        or getattr(client, "knowledge_signature", None) != self.signature
                    )
                ):
                    client.cancelled = True
                    client.process.terminate()
                    try:
                        client.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        raise ValueError("local_model_worker_stop_timeout") from None
                if (
                    client is None
                    or client.identity != self.configuration
                    or getattr(client, "cancelled", False)
                    or client.process.poll() is not None
                ):
                    client = local_vision.LocalClient(*self.configuration, self.log_path)
                    if model_signature(self.configuration[1]) != self.signature:
                        client.cancelled = True
                        if client.process.poll() is None:
                            client.process.terminate()
                        raise ValueError("local_knowledge_model_changed")
                    client.knowledge_signature = self.signature
                    local_vision._CLIENT = client
                yield
        finally:
            _SLOT.release()

    def infer(self, prompt, images=(), max_tokens=4096, schema=None):
        return local_vision._CLIENT.infer(prompt, images, max_tokens=max_tokens, schema=schema)
