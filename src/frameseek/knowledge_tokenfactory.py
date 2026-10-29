"""Budget-gated Token Factory adapter for the opt-in base-index worker.

The default runtime does not construct this class. A trusted operator must
provide matching extraction calibration; normal provider reservations remain
mandatory for every actual request.
"""

from pathlib import Path

from frameseek.agent import AgentError
from frameseek.cloud_vision import CloudClient
from frameseek.index_service import source_digest
from frameseek.knowledge_budget import admit_base_index_store, admit_supplement_store
from frameseek.knowledge_compiler import extraction_schema
from frameseek.occurrences import fingerprint


class TokenFactoryKnowledgeProcessor:
    def __init__(self, agent, source, owner, calibration, *, store, active):
        directory = Path(agent.ws.root) / source["id"]
        if (
            store.directory != directory
            or store.identity.video != source["id"]
            or store.identity.generation != source["generation"]
        ):
            raise ValueError("knowledge_store_source_mismatch")
        self.estimate = admit_base_index_store(
            agent.ledger, owner, agent.runtime, calibration, store
        )
        self.active = active
        runtime = agent.runtime
        self.revision = runtime.vision.revision
        self.extraction_config = store.identity.extraction_config
        provider = agent.provider(source["id"], owner, source["generation"])
        original_check = provider.check

        def check():
            original_check()
            if not active():
                raise AgentError("query_cancelled")

        provider.check = check
        identity = fingerprint(
            {
                "video": source["id"],
                "generation": source["generation"],
                "source_fingerprint": source_digest(directory),
                "revision": self.revision,
                "config": self.extraction_config,
                "calibration": calibration.reference,
            }
        )
        self.client = CloudClient(
            provider, owner, identity, directory, active, attempt_kind="coarse", category="ingest"
        )

    def infer(self, prompt, images=(), schema=None):
        if not self.active():
            raise AgentError("query_cancelled")
        if not 1 <= len(images) <= 8 or schema != extraction_schema(len(images)):
            raise AgentError("knowledge_extraction_contract_mismatch")
        return self.client.infer(prompt, images, max_tokens=4096, schema=schema)


class TokenFactorySupplementProcessor:
    cloud_only = True
    extraction_config = "knowledge-v3-8sec-8frames-4096tokens"

    def __init__(self, agent, source, owner, calibration, *, store, question, plan, active):
        directory = Path(agent.ws.root) / source["id"]
        if (
            store.directory != directory
            or store.identity.video != source["id"]
            or store.identity.generation != source["generation"]
        ):
            raise ValueError("knowledge_store_source_mismatch")
        self.active = active
        self.revision = agent.runtime.vision.revision
        self.estimate = admit_supplement_store(
            agent.ledger, owner, agent.runtime, calibration, store, question, plan, self
        )
        provider = agent.provider(source["id"], owner, source["generation"])
        original_check = provider.check

        def check():
            original_check()
            if not active():
                raise AgentError("query_cancelled")

        provider.check = check
        identity = fingerprint(
            {
                "source": store.identity.source_fingerprint,
                "generation": source["generation"],
                "revision": self.revision,
                "config": self.extraction_config,
                "calibration": calibration.reference,
                "contract": "single_frame_verified_v6",
            }
        )
        self.client = CloudClient(
            provider, owner, identity, directory, active, attempt_kind="zoom", category="query"
        )

    def infer(self, prompt, images=(), schema=None):
        from frameseek.knowledge_supplement import VERIFY_SCHEMA

        if not self.active():
            raise AgentError("query_cancelled")
        if len(images) != 1 or schema not in (extraction_schema(1), VERIFY_SCHEMA):
            raise AgentError("knowledge_supplement_contract_mismatch")
        return self.client.infer(prompt, images, max_tokens=4096, schema=schema)
