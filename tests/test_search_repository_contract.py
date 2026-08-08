from pathlib import Path


def test_search_repository_uses_hnsw_eligible_parameterized_cosine_sql() -> None:
    source = (Path(__file__).parents[1] / "app" / "repositories" / "search.py").read_text()

    assert "c.embedding <=> %(embedding)s::vector AS cosine_distance" in source
    assert "ORDER BY c.embedding <=> %(embedding)s::vector ASC" in source
    assert "LIMIT %(candidate_limit)s" in source
    assert "c.embedding_model = %(embedding_model)s" in source
    assert "lower(r.primary_language) = lower(%(language)s)" in source
    assert "r.stars >= %(minimum_stars)s" in source
    assert "iterative_scan" not in source
    assert "ef_search" not in source
    assert "enable_seqscan" not in source
