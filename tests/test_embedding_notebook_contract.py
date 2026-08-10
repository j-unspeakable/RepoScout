import json
import re
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
    assert 'os.environ["HF_HUB_DISABLE_XET"] = "1"' in code
    assert 'os.environ["HF_XET_CACHE"]' in code
    assert ".persist(" not in code
    assert "StorageLevel" not in code


def test_notebook_owns_oauth_and_pgvector_persistence_without_app_imports() -> None:
    code = _notebook_code()

    assert "WorkspaceClient()" in code
    assert "generate_database_credential" in code
    assert "localCheckpoint(eager=True)" in code
    assert "toLocalIterator()" in code
    assert "%s::vector" in code
    assert "processing_config_hash" in code
    assert "import psycopg2" in code
    assert '"psycopg2-binary==2.9.10"' in code
    assert "with closing(connect_lakebase()) as connection" in code
    assert "embedded_df.unpersist()" not in code
    assert "app.database" not in code
    assert "LakebaseCredentialProvider" not in code
    assert "dbutils.secrets" not in code
    assert "lakebase-url" not in code
    assert not any("password" in line.lower() and "print(" in line for line in code.splitlines())


def test_notebook_has_neutral_configuration_and_no_saved_environment_data() -> None:
    source = NOTEBOOK_PATH.read_text()
    notebook = json.loads(source)
    code = _notebook_code()

    assert 'os.getenv("LAKEBASE_ENDPOINT", "")' in code
    assert 'os.getenv("PGHOST", "")' in code
    assert 'os.getenv("PGUSER", "")' in code
    assert 'ensure_widget("max_repositories", "50"' in code
    assert re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", source, re.IGNORECASE) is None
    assert re.search(r"[\w.-]+\.database\.[\w.-]+\.cloud\.databricks\.com", source) is None
    assert re.search(r"projects/[^/\s]+/branches/[^/\s]+/endpoints/[^/\s]+", source) is None
    assert all(not cell.get("outputs") for cell in notebook["cells"])

    widgets = notebook["metadata"]["application/vnd.databricks.v1+notebook"]["widgets"]
    for name in ("lakebase_endpoint", "pg_host", "pg_user"):
        assert widgets[name]["currentValue"] == ""
        assert widgets[name]["typedWidgetInfo"]["defaultValue"] == ""
        assert widgets[name]["widgetInfo"]["defaultValue"] == ""
    assert widgets["max_repositories"]["currentValue"] == "50"
    assert widgets["max_repositories"]["typedWidgetInfo"]["defaultValue"] == "50"


def test_notebook_contains_runtime_transformation_and_embedding_assertions() -> None:
    code = _notebook_code()

    assert "_cleaned_fixture == \"Title\\n\\nDocs\\n\\nprint('ok')\"" in code
    assert '(0, "abcde"), (1, "defgh"), (2, "ghijk"), (3, "jkl")' in code
    assert "len(row.embedding) == EMBEDDING_DIM" in code
    assert "Validated {EMBEDDED_CHUNK_COUNT} embeddings" in code
