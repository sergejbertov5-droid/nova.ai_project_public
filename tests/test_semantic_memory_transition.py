"""Regression test: index consistency across full -> degraded -> full transitions.

Three isolated processes share one temporary directory:
1. Full backend: add records A and B (distinct content).
2. Degraded backend (dependencies blocked inside a subprocess): update A
   to new text, delete B, add C — total record count stays 2.
3. Full backend again: verify A updated, C present, B gone, and searches
   use fresh vectors, not stale ones.
"""
import os
import sys
import json
import subprocess
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ---------------------------------------------------------------------------
# Degraded phase: runs in a fresh subprocess so the parent pytest process
# never touches sys.modules.
# ---------------------------------------------------------------------------
_DEGRADED_SCRIPT = r"""
import os, sys, json
from pathlib import Path

# Block optional dependencies BEFORE importing the module so that the
# import side-effects (e.g. the module-level singleton) cannot load them.
sys.modules["numpy"] = None
sys.modules["faiss"] = None
sys.modules["sentence_transformers"] = None
sys.modules["sentence_transformers.SentenceTransformer"] = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.semantic_memory import (
    SemanticMemory,
    SemanticMemoryConfig,
    reset_semantic_memory,
    reset_semantic_memory_config,
)

tmp_dir = sys.argv[1]
rid_a = sys.argv[2]
rid_b = sys.argv[3]

cfg = SemanticMemoryConfig()
cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
reset_semantic_memory_config(cfg)
reset_semantic_memory(None)

mem = SemanticMemory()
assert mem._model is None
assert mem._index is None
assert len(mem._metadata) == 2

# Update A to substantially different text
ok = mem.update_text(
    rid_a,
    "The moon orbits the earth once every twenty seven days",
    {"source": "space", "tag": "moon"},
)
assert ok == rid_a

# Delete B
assert mem.delete(rid_b) is True

# Add C
rid_c = mem.add_text(
    "Cooking pasta requires boiling water and salt",
    {"source": "food", "tag": "pasta"},
)
assert rid_c
assert len(mem._metadata) == 2  # count unchanged

print("DEGRADED_OK", rid_c)
"""


def test_transition_full_degraded_full_index_consistency():
    if not _model_cached():
        pytest.skip("embedding model not in local HF cache – refusing to download")

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        cfg = SemanticMemoryConfig()
        cfg.config["index_path"] = str(tmp_dir / "test.index")
        cfg.config["metadata_path"] = str(tmp_dir / "test.json")
        reset_semantic_memory_config(cfg)
        reset_semantic_memory(None)

        # ------------------------------------------------------------
        # Process 1: full backend — create A and B
        # ------------------------------------------------------------
        mem1 = SemanticMemory()
        assert mem1._model is not None
        # The FAISS index is created lazily on the first add_text.
        assert mem1._index is None

        rid_a = mem1.add_text(
            "The quick brown fox jumps over the lazy dog",
            {"source": "animals", "tag": "fox"},
        )
        rid_b = mem1.add_text(
            "Machine learning is transforming artificial intelligence",
            {"source": "ai", "tag": "ml"},
        )
        assert mem1._index is not None
        assert mem1._index.ntotal == 2
        assert len(mem1._metadata) == 2

        # ------------------------------------------------------------
        # Process 2: degraded backend — update A, delete B, add C
        # ------------------------------------------------------------
        script_path = tmp_dir / "_degraded.py"
        script_path.write_text(_DEGRADED_SCRIPT, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        proc = subprocess.run(
            [sys.executable, str(script_path), str(tmp_dir), rid_a, rid_b],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"degraded subprocess failed (rc={proc.returncode}):\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        assert proc.stdout.startswith("DEGRADED_OK"), f"unexpected output: {proc.stdout}"
        rid_c = proc.stdout.strip().split()[1]

        # ------------------------------------------------------------
        # Process 3: full backend again — verify freshness
        # ------------------------------------------------------------
        reset_semantic_memory(None)
        reset_semantic_memory_config(cfg)

        mem3 = SemanticMemory()
        assert mem3._model is not None
        assert mem3._index is not None
        assert len(mem3._metadata) == 2

        # get_by_id checks
        rec_a = mem3.get_by_id(rid_a)
        assert "moon" in rec_a["text"].lower()
        assert rec_a.get("source") == "space"

        rec_c = mem3.get_by_id(rid_c)
        assert "pasta" in rec_c["text"].lower()
        assert rec_c.get("source") == "food"

        with pytest.raises(RecordNotFoundError):
            mem3.get_by_id(rid_b)

        # Search checks — fox should NOT return moon; moon query returns A; pasta returns C
        fox_res = mem3.search("quick brown fox", k=3)
        if fox_res:
            for hit in fox_res:
                assert "moon" not in hit["text"].lower(), (
                    f"Stale vector: fox query returned moon text: {hit['text'][:40]}"
                )

        moon_res = mem3.search("moon orbits earth", k=2)
        assert len(moon_res) >= 1
        assert moon_res[0]["id"] == rid_a

        pasta_res = mem3.search("boiling water pasta", k=2)
        assert len(pasta_res) >= 1
        assert pasta_res[0]["id"] == rid_c

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        reset_semantic_memory_config(None)
        reset_semantic_memory(None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])