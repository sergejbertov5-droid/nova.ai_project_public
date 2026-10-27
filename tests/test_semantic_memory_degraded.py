"""Minimal degraded-mode smoke test for SemanticMemory.

Runs WITHOUT numpy / faiss / sentence-transformers. Uses a temporary
directory and isolated state so it never touches real data.
"""
import os
import sys
import json
import subprocess
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.semantic_memory import (
    SemanticMemory,
    SemanticMemoryConfig,
    RecordNotFoundError,
    reset_semantic_memory,
    reset_semantic_memory_config,
)


# ---------------------------------------------------------------------------
# Helper: a tiny script that runs in a fresh Python process so the test
# can prove that data was written to disk by one process and survives a
# completely separate process restart.
# ---------------------------------------------------------------------------
_PERSIST_SCRIPT = r"""
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
    RecordNotFoundError,
    reset_semantic_memory,
    reset_semantic_memory_config,
)

tmp_dir = sys.argv[1]
action = sys.argv[2]
arg3 = sys.argv[3] if len(sys.argv) > 3 else None
arg4 = sys.argv[4] if len(sys.argv) > 4 else None

cfg = SemanticMemoryConfig()
cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
reset_semantic_memory_config(cfg)
reset_semantic_memory(None)

mem = SemanticMemory()
assert mem._is_ready is True
assert mem._model is None
assert mem._index is None

if action == "write":
    rid = mem.add_text("degraded persistence test", {"source": "unit-test", "tag": "persistence"})
    assert rid
    print("WROTE", rid)
elif action == "read":
    assert len(mem._metadata) == 1, f"expected 1 record, got {len(mem._metadata)}"
    rec = mem._metadata[0]
    assert rec["text"] == "degraded persistence test"
    assert rec.get("source") == "unit-test"
    assert rec.get("tag") == "persistence"
    print("READ_OK", rec["id"])
elif action == "update_delete":
    # Add two records: A and B
    rid_a = mem.add_text("original A", {"source": "A-source", "tag": "alpha"})
    rid_b = mem.add_text("original B", {"source": "B-source", "tag": "beta"})
    assert rid_a and rid_b
    # Update A: new text and metadata
    ok = mem.update_text(rid_a, "updated A", {"source": "updated-source"})
    assert ok == rid_a
    # Delete B
    ok = mem.delete(rid_b)
    assert ok is True
    print("UPDATE_DELETE_OK", rid_a, rid_b)
elif action == "read_after_update_delete":
    # arg3 = id_a, arg4 = id_b (unused here, but checked separately)
    assert arg3 and arg4
    rec_a = mem.get_by_id(arg3)
    assert rec_a["text"] == "updated A"
    assert rec_a.get("source") == "updated-source"
    try:
        mem.get_by_id(arg4)
    except RecordNotFoundError:
        pass
    else:
        raise AssertionError("get_by_id(B) did not raise RecordNotFoundError after delete")
    print("READ_AFTER_OK")
elif action == "clear_readd":
    # Add A, clear(), then add B
    rid_a = mem.add_text("original A", {"source": "A-source", "tag": "alpha"})
    assert rid_a
    mem.clear()
    rid_b = mem.add_text("original B", {"source": "B-source", "tag": "beta"})
    assert rid_b
    print("CLEAR_READD_OK", rid_a, rid_b)
elif action == "read_after_clear_readd":
    # arg3 = id_a, arg4 = id_b
    assert arg3 and arg4
    rec_b = mem.get_by_id(arg4)
    assert rec_b["text"] == "original B"
    assert rec_b.get("source") == "B-source"
    assert rec_b.get("tag") == "beta"
    try:
        mem.get_by_id(arg3)
    except RecordNotFoundError:
        pass
    else:
        raise AssertionError("get_by_id(A) did not raise RecordNotFoundError after clear()")
    print("READ_AFTER_CLEAR_OK")
else:
    raise SystemExit(f"unknown action {action}")
"""


def _run_persistence_subprocess(tmp_dir: str, action: str, *extra_args) -> str:
    """Run the persistence script in a fresh interpreter."""
    script_path = Path(tmp_dir) / "_persist_check.py"
    script_path.write_text(_PERSIST_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    # Build command: script tmp_dir action [extra...]
    cmd = [sys.executable, str(script_path), tmp_dir, action] + [str(a) for a in extra_args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"subprocess {action} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


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
action = sys.argv[2]
arg3 = sys.argv[3] if len(sys.argv) > 3 else None
arg4 = sys.argv[4] if len(sys.argv) > 4 else None

cfg = SemanticMemoryConfig()
cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
reset_semantic_memory_config(cfg)
reset_semantic_memory(None)

mem = SemanticMemory()
assert mem._is_ready is True
assert mem._model is None
assert mem._index is None

if action == "init":
    # Degraded initialization only.
    print("INIT_OK")
elif action == "write":
    rid = mem.add_text("degraded persistence test", {"source": "unit-test", "tag": "persistence"})
    assert rid
    print("WROTE", rid)
elif action == "read":
    assert len(mem._metadata) == 1, f"expected 1 record, got {len(mem._metadata)}"
    rec = mem._metadata[0]
    assert rec["text"] == "degraded persistence test"
    assert rec.get("source") == "unit-test"
    assert rec.get("tag") == "persistence"
    print("READ_OK", rec["id"])
elif action == "update_delete":
    # Add two records: A and B
    rid_a = mem.add_text("original A", {"source": "A-source", "tag": "alpha"})
    rid_b = mem.add_text("original B", {"source": "B-source", "tag": "beta"})
    assert rid_a and rid_b
    # Update A: new text and metadata
    ok = mem.update_text(rid_a, "updated A", {"source": "updated-source"})
    assert ok == rid_a
    # Delete B
    ok = mem.delete(rid_b)
    assert ok is True
    print("UPDATE_DELETE_OK", rid_a, rid_b)
elif action == "read_after_update_delete":
    # arg3 = id_a, arg4 = id_b
    assert arg3 and arg4
    rec_a = mem.get_by_id(arg3)
    assert rec_a["text"] == "updated A"
    assert rec_a.get("source") == "updated-source"
    try:
        mem.get_by_id(arg4)
    except RecordNotFoundError:
        pass
    else:
        raise AssertionError("get_by_id(B) did not raise RecordNotFoundError after delete")
    print("READ_AFTER_OK")
elif action == "clear_readd":
    # Add A, clear(), then add B
    rid_a = mem.add_text("original A", {"source": "A-source", "tag": "alpha"})
    assert rid_a
    mem.clear()
    rid_b = mem.add_text("original B", {"source": "B-source", "tag": "beta"})
    assert rid_b
    print("CLEAR_READD_OK", rid_a, rid_b)
elif action == "read_after_clear_readd":
    # arg3 = id_a, arg4 = id_b
    assert arg3 and arg4
    rec_b = mem.get_by_id(arg4)
    assert rec_b["text"] == "original B"
    assert rec_b.get("source") == "B-source"
    assert rec_b.get("tag") == "beta"
    try:
        mem.get_by_id(arg3)
    except RecordNotFoundError:
        pass
    else:
        raise AssertionError("get_by_id(A) did not raise RecordNotFoundError after clear()")
    print("READ_AFTER_CLEAR_OK")
else:
    raise SystemExit(f"unknown action {action}")
"""


def _run_degraded_subprocess(tmp_dir: str, action: str, *extra_args) -> str:
    """Run the degraded-mode script in a fresh interpreter.

    The parent pytest process never touches sys.modules: blocking of
    numpy / faiss / sentence_transformers happens only inside the
    subprocess, before the project code is imported.
    """
    script_path = Path(tmp_dir) / "_degraded_check.py"
    script_path.write_text(_DEGRADED_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    cmd = [sys.executable, str(script_path), tmp_dir, action] + [str(a) for a in extra_args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"subprocess {action} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def test_degraded_initialization():
    """In degraded mode the memory is ready but has no model and no index."""
    tmp_dir = tempfile.mkdtemp()
    try:
        out = _run_degraded_subprocess(tmp_dir, "init")
        assert out == "INIT_OK", f"unexpected output: {out}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_degraded_persistence_across_subprocess():
    """add_text in one process, then read the same record back in a fresh
    Python interpreter. Proves the data survived on disk, not in memory."""
    tmp_dir = tempfile.mkdtemp()
    try:
        out = _run_persistence_subprocess(tmp_dir, "write")
        assert out.startswith("WROTE"), f"unexpected write output: {out}"

        out = _run_persistence_subprocess(tmp_dir, "read")
        assert out.startswith("READ_OK"), f"unexpected read output: {out}"

        # The JSON file must exist and contain exactly one record.
        meta_path = Path(tmp_dir) / "test.json"
        assert meta_path.is_file(), "metadata file was not written to disk"
        with open(meta_path, "r", encoding="utf-8") as f:
            records = json.load(f)
        assert isinstance(records, list) and len(records) == 1
        assert records[0]["text"] == "degraded persistence test"
        assert records[0]["source"] == "unit-test"
        assert records[0]["tag"] == "persistence"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_degraded_update_delete_persistence():
    """Two records A/B in process 1; update A, delete B. In a fresh process
    verify A has the new text/metadata and B is gone (via the public API)."""
    tmp_dir = tempfile.mkdtemp()
    try:
        # --- process 1: write A, B, then update A and delete B ---
        script_path = Path(tmp_dir) / "_update_delete.py"
        script_path.write_text(_PERSIST_SCRIPT, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

        proc = subprocess.run(
            [sys.executable, str(script_path), tmp_dir, "update_delete"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"update_delete subprocess failed (rc={proc.returncode}):\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        assert proc.stdout.startswith("UPDATE_DELETE_OK"), f"unexpected output: {proc.stdout}"
        # Extract the IDs from stdout: "UPDATE_DELETE_OK <rid_a> <rid_b>"
        parts = proc.stdout.strip().split()
        assert len(parts) == 3
        rid_a, rid_b = parts[1], parts[2]

        # --- process 2: read A, confirm B is gone ---
        out = _run_persistence_subprocess(tmp_dir, "read_after_update_delete", rid_a, rid_b)
        assert out.startswith("READ_AFTER_OK"), f"unexpected output: {out}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_degraded_clear_readd_persistence():
    """Add A, clear(), then add B. In a fresh process verify B exists and A is gone."""
    tmp_dir = tempfile.mkdtemp()
    try:
        # --- process 1: add A, clear, add B ---
        script_path = Path(tmp_dir) / "_clear_readd.py"
        script_path.write_text(_PERSIST_SCRIPT, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

        proc = subprocess.run(
            [sys.executable, str(script_path), tmp_dir, "clear_readd"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"clear_readd subprocess failed (rc={proc.returncode}):\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        assert proc.stdout.startswith("CLEAR_READD_OK"), f"unexpected output: {proc.stdout}"
        parts = proc.stdout.strip().split()
        assert len(parts) == 3
        rid_a, rid_b = parts[1], parts[2]

        # --- process 2: verify B exists, A is gone ---
        out = _run_persistence_subprocess(tmp_dir, "read_after_clear_readd", rid_a, rid_b)
        assert out.startswith("READ_AFTER_CLEAR_OK"), f"unexpected output: {out}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


_DEGRADED_REMEMBER_SCRIPT = r"""
import os, sys, json
from pathlib import Path

# Block optional dependencies BEFORE importing the module so that the
# import side-effects (e.g. the module-level singleton) cannot load them.
sys.modules["numpy"] = None
sys.modules["faiss"] = None
sys.modules["sentence_transformers"] = None
sys.modules["sentence_transformers.SentenceTransformer"] = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.memory import MemoryStore
from core.semantic_memory import (
    SemanticMemory,
    SemanticMemoryConfig,
    reset_semantic_memory,
    reset_semantic_memory_config,
)

tmp_dir = sys.argv[1]
fact = sys.argv[2]

cfg = SemanticMemoryConfig()
cfg.config["index_path"] = str(Path(tmp_dir) / "test.index")
cfg.config["metadata_path"] = str(Path(tmp_dir) / "test.json")
reset_semantic_memory_config(cfg)
reset_semantic_memory(None)

store = MemoryStore(path=str(Path(tmp_dir) / "memory.json"))
answer = store.remember(fact, source="command")
assert answer == "Запомнила.", answer

# MemoryStore must have written the fact to its own file.
with open(Path(tmp_dir) / "memory.json", "r", encoding="utf-8") as f:
    memories = json.load(f)
assert isinstance(memories, list) and len(memories) == 1, memories
assert memories[0]["fact"] == fact, memories

# SemanticMemory must be ready in degraded mode but without a model/index.
sm = SemanticMemory()
assert sm._is_ready is True
assert sm._model is None
assert sm._index is None
# The fact text is still persisted in the metadata sidecar.
assert any(e.get("text") == fact for e in sm._metadata), sm._metadata
print("DEGRADED_REMEMBER_OK")
"""


def test_remember_semantic_skipped_when_unavailable():
    """When numpy / faiss / sentence-transformers are absent, MemoryStore.remember()
    must NOT crash — it writes to data/memory.json and the semantic sidecar,
    and reports the skip through the degraded mode (no model, no index)."""
    tmp_dir = tempfile.mkdtemp()
    try:
        script_path = Path(tmp_dir) / "_degraded_remember.py"
        script_path.write_text(_DEGRADED_REMEMBER_SCRIPT, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        result = subprocess.run(
            [sys.executable, str(script_path), tmp_dir, "сэр любит чай"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"degraded remember subprocess failed (rc={result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        assert result.stdout.strip() == "DEGRADED_REMEMBER_OK", (
            f"unexpected output: {result.stdout}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])