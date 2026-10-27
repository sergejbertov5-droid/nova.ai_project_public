"""Full-backend smoke test for SemanticMemory using the locally cached model.

Runs ONLY when numpy / faiss / sentence-transformers are actually installed
AND the configured embedding model is already present in the local HuggingFace
cache.  Nothing is downloaded.  If either condition is missing the test is
skipped with an explicit reason.
"""
import os
import sys
import json
import subprocess
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.memory import MemoryStore
from core.semantic_memory import (
    SemanticMemory,
    SemanticMemoryConfig,
    RecordNotFoundError,
    reset_semantic_memory,
    reset_semantic_memory_config,
)


def _model_cached() -> bool:
    """True when the configured embedding model is already in the HF cache."""
    cfg = SemanticMemoryConfig()
    name = cfg.config["embedding_model_name"]
    safe = name.replace("/", "--")
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    return (cache_dir / f"models--{safe}").is_dir()


def _deps_available() -> bool:
    """True when numpy / faiss / sentence-transformers are actually installed.

    Uses importlib.util.find_spec, which checks whether a module can be
    found WITHOUT importing it.  Re-importing numpy after its
    C-extensions are already loaded segfaults the interpreter, so we must
    never do that here.
    """
    import importlib.util
    for mod in ("numpy", "faiss", "sentence_transformers"):
        try:
            if importlib.util.find_spec(mod) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


pytestmark = pytest.mark.skipif(
    not _deps_available(),
    reason="numpy / faiss / sentence-transformers not installed",
)


def test_full_backend_clear_readd_persistence():
    """Full backend: add A, clear(), add B, then reload in a fresh process.
    B must be found by search and get_by_id; A must be gone."""
    if not _model_cached():
        pytest.skip(
            "embedding model not in local HF cache – refusing to download"
        )

    tmp_dir = tempfile.mkdtemp()
    try:
        cfg = SemanticMemoryConfig()
        cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
        cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
        reset_semantic_memory_config(cfg)
        reset_semantic_memory(None)

        mem = SemanticMemory()
        assert mem._model is not None
        # The FAISS index is created lazily on the first add_text, so we
        # only assert it exists after a record has been added.
        rid_a = mem.add_text("original A", {"source": "A-source", "tag": "alpha"})
        assert rid_a
        assert mem._index is not None
        assert mem._index.ntotal == 1
        mem.clear()
        rid_b = mem.add_text("original B", {"source": "B-source", "tag": "beta"})
        assert rid_b
        assert mem._index is not None
        assert mem._index.ntotal == 1

        # search must find B
        results = mem.search("original B", k=3)
        assert any(r["text"] == "original B" for r in results), results

        # A must be gone
        with pytest.raises(RecordNotFoundError):
            mem.get_by_id(rid_a)

        # B must be retrievable
        rec_b = mem.get_by_id(rid_b)
        assert rec_b["text"] == "original B"
        assert rec_b.get("source") == "B-source"
        assert rec_b.get("tag") == "beta"

        # Persist to disk and reload in a fresh process
        script_path = Path(tmp_dir) / "_full_reload.py"
        script_path.write_text(_FULL_RELOAD_SCRIPT, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        proc = subprocess.run(
            [sys.executable, str(script_path), tmp_dir, rid_b, rid_a],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"reload subprocess failed (rc={proc.returncode}):\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        assert proc.stdout.startswith("RELOAD_OK"), f"unexpected output: {proc.stdout}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        reset_semantic_memory_config(None)
        reset_semantic_memory(None)


_FULL_RELOAD_SCRIPT = r"""
import os, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.semantic_memory import (
    SemanticMemory,
    SemanticMemoryConfig,
    RecordNotFoundError,
    reset_semantic_memory,
    reset_semantic_memory_config,
)

tmp_dir = sys.argv[1]
rid_b = sys.argv[2]
rid_a = sys.argv[3]

cfg = SemanticMemoryConfig()
cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
reset_semantic_memory_config(cfg)
reset_semantic_memory(None)

mem = SemanticMemory()
assert mem._model is not None
# The FAISS index is created lazily on the first add_text.
assert mem._index is None or mem._index.ntotal >= 0

# B must survive
results = mem.search("original B", k=3)
assert any(r["text"] == "original B" for r in results), results
rec_b = mem.get_by_id(rid_b)
assert rec_b["text"] == "original B"
assert rec_b.get("source") == "B-source"
assert rec_b.get("tag") == "beta"

# A must be gone
try:
    mem.get_by_id(rid_a)
except RecordNotFoundError:
    pass
else:
    raise AssertionError("get_by_id(A) did not raise RecordNotFoundError after clear()")

print("RELOAD_OK")
"""


def test_remember_doubles_in_semantic_memory():
    """MemoryStore.remember() must also write the fact into SemanticMemory,
    so that a semantic (vector) search can find it — not just word-overlap."""
    if not _model_cached():
        pytest.skip("embedding model not in local HF cache – refusing to download")

    tmp_dir = tempfile.mkdtemp()
    try:
        cfg = SemanticMemoryConfig()
        cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
        cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
        reset_semantic_memory_config(cfg)
        reset_semantic_memory(None)

        store = MemoryStore(path=str(Path(tmp_dir) / "memory.json"))
        store.remember(
            "сэр любит крепкий кофе",
            important=True,
            category="preference",
            source="command",
            confidence=1.0,
        )

        sm = SemanticMemory()
        assert sm._model is not None
        assert sm._index is not None
        assert sm._index.ntotal >= 1

        # Семантический поиск по синониму (не точное совпадение слов).
        results = sm.search("кофе по утрам", k=3)
        assert results, "semantic search returned no results after remember()"
        assert any("кофе" in r["text"] for r in results), results
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        reset_semantic_memory_config(None)
        reset_semantic_memory(None)


def test_repeated_remember_no_duplicate_in_semantic():
    """Calling remember() twice with the same fact must not grow the
    SemanticMemory index — the second call should update_text(), not add_text()."""
    if not _model_cached():
        pytest.skip("embedding model not in local HF cache – refusing to download")

    tmp_dir = tempfile.mkdtemp()
    try:
        cfg = SemanticMemoryConfig()
        cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
        cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
        reset_semantic_memory_config(cfg)
        reset_semantic_memory(None)

        store = MemoryStore(path=str(Path(tmp_dir) / "memory.json"))
        store.remember("сэр любит чай", source="command")
        store.remember("сэр любит чай", source="command")

        sm = SemanticMemory()
        assert sm._model is not None
        assert sm._index is not None
        # Один факт — одна векторная запись, не две.
        assert sm._index.ntotal == 1, (
            f"expected 1 vector, got {sm._index.ntotal} (duplicate created)"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        reset_semantic_memory_config(None)
        reset_semantic_memory(None)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])