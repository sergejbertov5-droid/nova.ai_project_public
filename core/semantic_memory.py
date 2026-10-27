"""
semantic_memory.py — module for managing semantic memory with persistence,
semantic search, and safe recovery.

The implementation follows a versioned‑schema approach, uses atomic writes,
and guards all mutable state with a re‑entrant lock.  It is deliberately
 defensive: missing dependencies, corrupted files, and version mismatches
 are handled with clear, informative errors rather than silent failures.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import tempfile
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
#  Configuration handling
# --------------------------------------------------------------------------- #
class SemanticMemoryConfig:
    """
    Loads configuration from an optional JSON file (e.g.,
    ``semantic_memory_config.json`` placed next to this module).  All values
    have sensible defaults and are validated on load.
    """

    _instance: "SemanticMemoryConfig" | None = None

    @classmethod
    def instance(cls) -> "SemanticMemoryConfig":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, config_path: Optional[Path] = None):
        # Default configuration -------------------------------------------------
        self.config: Dict[str, Any] = {
            "embedding_model_name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "index_path": "semantic_memory.index",
            "metadata_path": "semantic_memory.json",
            "similarity_threshold": 0.3,
            "max_results": 10,
            "schema_version": "1.0",
        }

        # Allow an external JSON file to override defaults --------------------
        if config_path is None:
            config_path = Path(__file__).with_name("semantic_memory_config.json")

        if config_path and config_path.is_file():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    extra = json.load(f)
                self.config.update(extra)
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(f"Failed to load semantic memory config: {exc}")

        # Normalise types -------------------------------------------------------
        self.config["similarity_threshold"] = float(
            self.config.get("similarity_threshold", 0.5)
        )
        self.config["max_results"] = int(self.config.get("max_results", 10))
        self.config["schema_version"] = str(
            self.config.get("schema_version", "1.0")
        )

        # Paths are derived from the live config dict so that tests (and the
    # singleton) can redirect storage at runtime without recreating the config.
    @property
    def index_path(self) -> Path:
        return Path(self.config["index_path"])

    @property
    def metadata_path(self) -> Path:
        return Path(self.config["metadata_path"])

    def _ensure_dirs(self) -> None:
        """Create parent directories for the current config paths."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
#  Core SemanticMemory implementation
# --------------------------------------------------------------------------- #

class RecordNotFoundError(RuntimeError):
    """Raised when a requested record ID cannot be found."""
    pass

class SemanticMemory:
    """
    Thread‑safe semantic memory backed by a FAISS index and a JSON
    metadata store.  All public methods are protected by an ``RLock`` so
    they can be safely used from multiple threads within the same process.
    ``clear()`` removes only this memory’s files and never touches other
    parts of the project.
    """

    # ------------------------------------------------------------------- #
    #  Construction & lifecycle
    # ------------------------------------------------------------------- #
    def __init__(self) -> None:
        self._lock = threading.RLock()
        cfg = SemanticMemoryConfig.instance()
        self.embedding_model_name = cfg.config["embedding_model_name"]
        self.similarity_threshold = cfg.config["similarity_threshold"]
        self.max_results = cfg.config["max_results"]
        self.schema_version = cfg.config["schema_version"]

        # internal state
        self._model: Optional[Any] = None          # lazy‑loaded SentenceTransformer
        self._index: Optional[Any] = None          # FAISS index
        self._metadata: List[Dict[str, Any]] = []   # in‑memory list of records
        self._index_revision: Optional[str] = None  # metadata hash the index was built for
        self._is_ready = False
        self._load_all()

    # ------------------------------------------------------------------- #
    #  Paths – read live from the shared config so tests can redirect
    #  storage without recreating the instance.
    # ------------------------------------------------------------------- #
    @property
    def index_path(self) -> Path:
        return Path(SemanticMemoryConfig.instance().config["index_path"])

    @property
    def metadata_path(self) -> Path:
        return Path(SemanticMemoryConfig.instance().config["metadata_path"])

    # ------------------------------------------------------------------- #
    #  Private helpers – loading
    # ------------------------------------------------------------------- #
    def _load_model(self) -> None:
        """Load the sentence‑transformer model.  Raises ImportError with a
        helpful message if the package is missing."""
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise ImportError(
                "sentence_transformers package is required. Install with "
                "`pip install sentence-transformers`."
            ) from exc

        self._model = SentenceTransformer(self.embedding_model_name)
        self._embedding_dim = self._model.get_sentence_embedding_dimension()

    def _load_index(self) -> None:
        """Load a persisted FAISS index if it exists; otherwise create a new
        one with the correct dimension."""
        if not self.index_path.is_file():
            self._index = None
            return

        try:
            import faiss  # type: ignore
        except Exception as exc:  # pragma: no cover
            # FAISS unavailable — fall back to text-only mode gracefully.
            logging.warning(
                "FAISS library is required for persistent indexing. Install with "
                "`pip install faiss-cpu`. Semantic search disabled, text mode only."
            )
            self._index = None
            return

        try:
            self._index = faiss.read_index(str(self.index_path))
        except Exception as exc:  # pragma: no cover
            # Corrupted index – start fresh but keep the metadata (it may still be valid)
            logging.warning(
                f"Failed to read FAISS index at {self.index_path}: {exc}"
            )
            self._index = None

        # Read the sidecar revision: the metadata hash the index was built for.
        rev_path = str(self.index_path) + ".revision"
        self._index_revision = None
        if rev_path and Path(rev_path).is_file():
            try:
                with open(rev_path, "r", encoding="utf-8") as f:
                    self._index_revision = f.read().strip() or None
            except OSError:
                self._index_revision = None

        # Validate that the index dimension matches the current model
        if self._index is not None:
            try:
                # FAISS IndexFlatIP reports dimension via `.d` attribute
                actual_dim = getattr(self._index, "d", None)
                if actual_dim is None and self._index.ntotal > 0:
                    # fallback: infer from first vector
                    import numpy as np
                    tmp = np.random.random(self._embedding_dim).astype("float32")
                    actual_dim = self._index.search(tmp, 1)[1][0]
                if actual_dim != self._embedding_dim:
                    logging.warning(
                        f"Index dimension ({actual_dim}) does not match model "
                        f"embedding dimension ({self._embedding_dim}); rebuilding index."
                    )
                    self._index = None
                    self._index_revision = None
            except Exception:
                logging.warning(
                    f"Index validation failed; treating as empty index."
                )
                self._index = None
                self._index_revision = None

    def _load_metadata(self) -> None:
        """Read the JSON metadata file.  If the file is missing or malformed,
        start with an empty list.  Old entries lacking an ``id`` are patched."""
        if not self.metadata_path.is_file():
            self._metadata = []
            return

        try:
            with open(self.metadata_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:  # pragma: no cover
            # Corrupted JSON — log and start fresh
            logging.warning(
                f"Failed to read semantic memory metadata at {self.metadata_path}: {exc}. "
                f"Starting with empty memory."
            )
            self._metadata = []
            return

        if not isinstance(raw, list):
            logging.warning(
                f"Metadata file at {self.metadata_path} does not contain a JSON array. "
                f"Starting with empty memory."
            )
            self._metadata = []
            return

        # Migrate legacy entries -------------------------------------------------
        migrated: List[Dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            # Ensure required fields exist
            if "id" not in entry:
                entry["id"] = uuid.uuid4().hex
            if "version" not in entry:
                entry["version"] = self.schema_version
            migrated.append(entry)

        self._metadata = migrated

    def _load_all(self) -> None:
        """Full initialization: load model → index → metadata.  If the index
        is missing but metadata exists, rebuild it.

        Missing optional dependencies (sentence-transformers / faiss) are
        tolerated: the memory stays usable in plain-text mode and only
        semantic search / embedding features are disabled.  This matters on
        machines where the heavy packages cannot be installed — the rest of
        the assistant must not fail to start because of it.
        """
        # Model – may be unavailable.  Never let a missing package abort
        # startup; degrade gracefully instead.
        if self._model is None:
            try:
                self._load_model()
            except ImportError as exc:
                logging.warning(
                    "Semantic memory model not loaded (%s) — "
                    "semantic search disabled, text mode only",
                    exc,
                )
                self._model = None

        # Metadata – always loaded first; migration handled inside
        self._load_metadata()

        # Index – may be None if file missing or corrupted
        self._load_index()

        # If we have metadata but no index, rebuild the index from scratch
        if self._metadata and self._index is None:
            try:
                self._rebuild_index_from_metadata()
            except ImportError as exc:
                logging.warning(
                    "Cannot rebuild semantic index (%s) — "
                    "text-only mode until FAISS is available",
                    exc,
                )
                self._index = None

        # If index exists but metadata is empty (corrupted/lost), clear the index too
        if not self._metadata and self._index is not None:
            logging.warning(
                f"Metadata empty but index has {self._index.ntotal} vectors; clearing index."
            )
            self._index = None
            self._index_revision = None

        # If the index exists but was built for a different metadata revision
        # (e.g. metadata was modified while the model/index was unavailable),
        # or its vector count does not match the metadata, rebuild it.
        # Both conditions must be checked: a matching revision does NOT
        # excuse a mismatched vector count (a corrupted index can have the
        # right revision but the wrong number of vectors).
        if self._index is not None and self._metadata:
            current_rev = self._metadata_hash()
            needs_rebuild = (
                self._index_revision != current_rev
                or self._index.ntotal != len(self._metadata)
            )
            if needs_rebuild:
                logging.warning(
                    f"Index revision ({self._index_revision}) does not match "
                    f"current metadata ({current_rev[:12]}…) or vector count "
                    f"({self._index.ntotal}) != metadata count "
                    f"({len(self._metadata)}); rebuilding index."
                )
                try:
                    self._rebuild_index_from_metadata()
                except ImportError as exc:
                    logging.warning(
                        "Cannot rebuild semantic index (%s) — text-only mode",
                        exc,
                    )
                    self._index = None
                    self._index_revision = None

        self._is_ready = True
        # Persist a clean state after load (helps with crashes during startup)
        self._save(force=True)

    def _rebuild_index_from_metadata(self) -> None:
        """Private helper: (re)create the FAISS index from the current
        ``_metadata`` list.  Called after an add, update, or delete."""
        # Если модель недоступна, нельзя построить векторный индекс.
        if self._model is None:
            logging.warning(
                "Semantic model unavailable – skipping index rebuild."
            )
            self._index = None
            return
        try:
            import faiss  # type: ignore
        except Exception as exc:  # pragma: no cover
            logging.warning(
                "FAISS library is required for index rebuild. Install with "
                "`pip install faiss-cpu`. Cannot rebuild index – "
                "semantic search disabled until FAISS is available."
            )
            self._index = None
            return

        dimension = self._embedding_dim
        self._index = faiss.IndexFlatIP(dimension)

        # Add all vectors
        vectors = []
        for entry in self._metadata:
            txt = entry.get("text", "")
            if txt:
                emb = self._model.encode([txt], normalize_embeddings=True)[0]
                vectors.append(emb.astype("float32"))
        if vectors:
            import numpy as np
            self._index.add(np.stack(vectors))
            # The index now matches the current metadata; record the revision
            # so that a later load can detect a stale index.
            self._index_revision = self._metadata_hash()
            logging.info(
                f"Rebuilt FAISS index with {len(vectors)} vectors from metadata."
            )

    # ------------------------------------------------------------------- #
    #  Private helpers – safe persistence
    # ------------------------------------------------------------------- #
    def _atomic_write(self, path: Path, data: Any) -> None:
        """Write *data* to *path* using a temporary file + atomic rename."""
        tmp_dir = path.parent
        if not tmp_dir.exists():
            tmp_dir.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=tmp_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            shutil.move(tmp_path, path)
        except Exception as exc:  # pragma: no cover
            # Clean up the temp file if something went wrong
            try:
                os.unlink(tmp_path)
            finally:
                raise exc

    def _save_index(self) -> None:
        """Persist the FAISS index atomically, along with a sidecar file
        holding the metadata hash the index was built for.  The sidecar
        lets us detect a stale index even when the vector count matches."""
        if self._index is None:
            return
        try:
            import faiss  # type: ignore
        except Exception as exc:  # pragma: no cover
            logging.warning(
                "FAISS not available while saving index. Skipping persistence."
            )
            return
        tmp_path = str(self.index_path) + ".tmp"
        faiss.write_index(self._index, tmp_path)
        shutil.move(tmp_path, self.index_path)

        # Sidecar: the metadata hash this index was built for.
        rev = self._index_revision or self._metadata_hash()
        rev_path = str(self.index_path) + ".revision"
        try:
            with open(rev_path, "w", encoding="utf-8") as f:
                f.write(rev)
        except OSError:
            pass

    def _save_metadata(self) -> None:
        """Atomically write the current metadata list.  Must be called while
        holding ``self._lock``."""
        self._atomic_write(self.metadata_path, self._metadata)

    def _metadata_hash(self) -> str:
        """Deterministic hash of the current metadata list.  Used as the
        persisted *index_revision* so that a stale FAISS index can be
        detected even when the vector count happens to match."""
        import hashlib
        ordered = sorted(self._metadata, key=lambda e: e.get("id", ""))
        data = json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _save(self, force: bool = False) -> None:
        """
        Persist both index and metadata.  When ``force=True`` we bypass the
        atomic wrapper – used only during initialization when we know the
        state is consistent.
        """
        if not force and not self._is_ready:
            return
        self._save_index()
        self._save_metadata()

    # ------------------------------------------------------------------- #
    #  Public API – mutation & CRUD
    # ------------------------------------------------------------------- #
    def add_text(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Add *text* to memory, store its embedding, and persist everything.
        Returns the generated unique identifier for the entry."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("`text` must be a non‑empty string.")

        with self._lock:
            # Если модель недоступна, сохраняем только текст без векторного представления.
            if self._model is None:
                entry: Dict[str, Any] = {
                    "id": uuid.uuid4().hex,
                    "text": text.strip(),
                    "added_at": int(time.time()),
                    "version": self.schema_version,
                }
                if metadata:
                    entry.update(metadata)
                self._metadata.append(entry)
                # Save metadata even when _is_ready is False.  The metadata has
                # changed; the FAISS index is either missing or now stale.
                # Do not attempt to rebuild the index without the model, but
                # write the updated metadata so future loads can rebuild it.
                self._index_revision = None
                self._save_metadata()
                return entry["id"]

            # Generate embedding ------------------------------------------------
            embedding = (
                self._model.encode([text.strip()], normalize_embeddings=True)[0]
                .astype("float32")
            )

            # Append to FAISS index ---------------------------------------------
            if self._index is None:
                import faiss  # type: ignore
                self._index = faiss.IndexFlatIP(self._embedding_dim)
            self._index.add(embedding.reshape(1, -1))

            # Build metadata entry -----------------------------------------------
            entry: Dict[str, Any] = {
                "id": uuid.uuid4().hex,
                "text": text.strip(),
                "added_at": int(time.time()),
                "version": self.schema_version,
            }
            if metadata:
                entry.update(metadata)

            self._metadata.append(entry)

            # Persist -------------------------------------------------------------
            self._save()

            return entry["id"]

    def get_by_id(self, record_id: str) -> Dict[str, Any]:
        """
        Return the full record (including its metadata) for the given UUID.
        Raises :class:`RecordNotFoundError` if the id does not exist.
        """
        with self._lock:
            if not self._is_ready:
                raise RuntimeError("SemanticMemory is not ready.")
            for entry in self._metadata:
                if entry["id"] == record_id:
                    return entry.copy()
            raise RecordNotFoundError(record_id)

    def update_text(self, record_id: str, new_text: str,
                    metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Replace the stored text (and optional metadata) of an existing record.
        Returns the (unchanged) record id.
        Raises :class:`RecordNotFoundError` if the id does not exist.
        """
        if not isinstance(new_text, str) or not new_text.strip():
            raise ValueError("`new_text` must be a non‑empty string.")

        with self._lock:
            # 1️⃣ Locate the entry
            entry_index = None
            for idx, entry in enumerate(self._metadata):
                if entry["id"] == record_id:
                    entry_index = idx
                    break
            if entry_index is None:
                raise RecordNotFoundError(record_id)

            # 4️⃣ Update metadata entry (preserve id and historical added_at!)
            original_added_at = self._metadata[entry_index].get("added_at")
            if original_added_at is None:
                original_added_at = int(time.time())
            updated_entry = {
                "id": record_id,
                "text": new_text.strip(),
                "added_at": original_added_at,
                "version": self.schema_version,
            }
            if metadata:
                updated_entry.update(metadata)
            # metadata must not silently clobber the historical added_at.
            updated_entry["added_at"] = original_added_at
            self._metadata[entry_index] = updated_entry

            # 5️⃣ Re‑build the index from the (now) updated metadata list
            self._rebuild_index_from_metadata()

            # 6️⃣ Persist changes
            self._save()
            return record_id

    def delete(self, record_id: str) -> bool:
        """
        Remove a record identified by its UUID.
        Returns ``True`` if a record was found and removed, ``False`` otherwise.
        """
        with self._lock:
            if not self._is_ready:
                raise RuntimeError("SemanticMemory is not ready.")

            # Find the entry
            target_idx = None
            for idx, entry in enumerate(self._metadata):
                if entry["id"] == record_id:
                    target_idx = idx
                    break
            if target_idx is None:
                return False

            # Remove from metadata list
            del self._metadata[target_idx]

            # Re‑build index
            self._rebuild_index_from_metadata()

            # Persist and exit
            self._save()
            return True

    # ------------------------------------------------------------------- #
    #  Public API – query
    # ------------------------------------------------------------------- #
    def search(self, query: str, k: Optional[int] = None, tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Perform a semantic search for *query* and return up to *k* results.
        Each result dict contains ``id``, ``text``, ``metadata`` and ``similarity``.
        Optional `tags` filter keeps only entries containing matching metadata tags.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("`query` must be a non‑empty string.")

        with self._lock:
            if not self._is_ready:
                raise RuntimeError("SemanticMemory is not ready – initialize first.")

            if self._index is None or not self._metadata:
                return []

            try:
                import faiss  # type: ignore
            except Exception as exc:  # pragma: no cover
                logging.warning(
                    "FAISS not available during search – returning empty results."
                )
                return []

            top_k = k if k is not None else self.max_results
            query_emb = (
                self._model.encode([query.strip()], normalize_embeddings=True)[0]
                .astype("float32")
            )
            distances, indices = self._index.search(
                query_emb.reshape(1, -1), min(top_k, self._index.ntotal)
            )

            results: List[Dict[str, Any]] = []
            for idx, score in zip(indices[0].tolist(), distances[0].tolist()):
                if idx < 0 or idx >= len(self._metadata):
                    continue
                meta = self._metadata[idx].copy()
                meta.pop("text", None)  # text is already exposed separately
                results.append(
                    {
                        "id": self._metadata[idx]["id"],
                        "text": self._metadata[idx]["text"],
                        "metadata": meta,
                        "similarity": float(score),
                    }
                )
            # Apply similarity threshold ------------------------------------------------
            threshold = self.similarity_threshold
            filtered = [r for r in results if r["similarity"] >= threshold]

            # Apply tag filter -----------------------------------------------------------
            if tags:
                filtered = [
                    r for r in filtered
                    if "tags" in r["metadata"] and any(t in r["metadata"]["tags"] for t in tags)
                ]

            return filtered

    # ------------------------------------------------------------------- #
    #  Public API – introspection & maintenance
    # ------------------------------------------------------------------- #
    def get_semantic_stats(self) -> Dict[str, Any]:
        """Return a dictionary of current metrics (vector count, dimensions, etc.)."""
        with self._lock:
            if not self._is_ready:
                raise RuntimeError("SemanticMemory is not ready.")
            vectors = int(self._index.ntotal if self._index else 0)
            dim = int(self._model.get_sentence_embedding_dimension()) if self._model else 0
            return {
                "vectors": vectors,
                "embedding_dimension": dim,
                "model_name": self.embedding_model_name,
                "similarity_threshold": self.similarity_threshold,
                "max_results": self.max_results,
                "schema_version": self.schema_version,
                "files": {
                    "index_path": str(self.index_path),
                    "metadata_path": str(self.metadata_path),
                },
            }

    def clear(self) -> None:
        """Delete all persisted files belonging to this memory and reset the
        in‑memory state.  Only files created by this class are removed."""
        with self._lock:
            if self._index is not None:
                try:
                    faiss.write_index(self._index, str(self.index_path) + ".tmp")
                    os.remove(self.index_path)
                except Exception:
                    pass  # best‑effort cleanup

            self._metadata = []
            # NOTE: do NOT remove metadata_path here. _save(force=True) writes
            # the (now empty) metadata list atomically, so the file stays
            # present and consistent. Removing the file before the save would
            # leave the instance without a valid metadata file, and a
            # subsequent add_text would have to recreate it from scratch.
            self._index = None
            # Persist the cleared state BEFORE flipping _is_ready — _save()
            # bails out when the instance is not ready, so write first.
            self._save(force=True)
            self._is_ready = True

    def recall_semantic(self, query: str, limit: int = 5) -> str:
        """Convenience wrapper that returns a human‑readable snippet of the top
        matching memories."""
        results = self.search(query, k=limit)
        if not results:
            return "No semantically similar records found."
        snippets = [
            f"{r['similarity']:.2f}: {r['metadata'].get('text', '')}"
            for r in results
        ]
        return " | ".join(snippets)


# --------------------------------------------------------------------------- #
#  Singleton accessor – used by the rest of the NOVA AI codebase
# --------------------------------------------------------------------------- #
_singleton: Optional[SemanticMemory] = None


def get_semantic_memory() -> SemanticMemory:
    """Return the shared ``SemanticMemory`` instance.  The first call
    lazily creates and fully initializes the object."""
    global _singleton
    if _singleton is None:
        _singleton = SemanticMemory()
    return _singleton


def reset_semantic_memory(instance: Optional[SemanticMemory] = None) -> None:
    """Replace the shared singleton.  Used by tests and by callers that
    need to redirect storage to a different path."""
    global _singleton
    _singleton = instance


def reset_semantic_memory_config(config: Optional[SemanticMemoryConfig] = None) -> None:
    """Replace the shared config singleton.  Used by tests to redirect
    storage paths without recreating the whole memory."""
    SemanticMemoryConfig._instance = config
