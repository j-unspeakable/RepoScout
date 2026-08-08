import json
from pathlib import Path

NOTEBOOK_PATH = Path(__file__).parents[1] / "notebook" / "process_repository_embeddings.ipynb"


def _notebook_code() -> str:
    notebook = json.loads(NOTEBOOK_PATH.read_text())
    assert notebook["nbformat"] == 4
    return "\n".join(
        "".join(cell.get("source", [])) for cell in notebook["cells"] if cell["cell_type"] == "code"
    )


def test_notebook_uses_spark_for_processing_and_batched_embeddings() -> None:
    code = _notebook_code()

    assert 'spark.read.format("jdbc")' in code
    assert "repositories_df.join(readmes_df" in code
    assert "left_anti" in code
    assert "F.regexp_replace" in code
    assert "F.sequence" in code
    assert "F.explode" in code
    assert "@pandas_udf(ArrayType(FloatType()))" in code
    assert "normalize_embeddings=True" in code
    assert "EMBEDDING_DIM = 384" in code


def test_notebook_owns_oauth_and_pgvector_persistence_without_app_imports() -> None:
    code = _notebook_code()

    assert "WorkspaceClient()" in code
    assert "generate_database_credential" in code
    assert "localCheckpoint(eager=True)" in code
    assert "toLocalIterator()" in code
    assert "%s::vector" in code
    assert "processing_config_hash" in code
    assert "app.database" not in code
    assert "LakebaseCredentialProvider" not in code
    assert "dbutils.secrets" not in code
    assert "lakebase-url" not in code
    assert not any("password" in line.lower() and "print(" in line for line in code.splitlines())


def test_notebook_contains_runtime_transformation_and_embedding_assertions() -> None:
    code = _notebook_code()

    assert "_cleaned_fixture == \"Title\\n\\nDocs\\n\\nprint('ok')\"" in code
    assert '(0, "abcde"), (1, "defgh"), (2, "ghijk"), (3, "jkl")' in code
    assert "len(row.embedding) == EMBEDDING_DIM" in code
    assert "Validated {EMBEDDED_CHUNK_COUNT} embeddings" in code
