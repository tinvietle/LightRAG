from pathlib import Path

from lightrag.kg import ner


def test_get_ner_model_cache_dir_defaults_to_top_level_directory(monkeypatch) -> None:
    monkeypatch.delenv(ner.NER_MODEL_DIR_ENV, raising=False)

    assert ner.get_ner_model_cache_dir() == Path("./ner_model")


def test_get_ner_model_cache_dir_honors_env_override(monkeypatch, tmp_path) -> None:
    custom_dir = tmp_path / "gliner-cache"
    monkeypatch.setenv(ner.NER_MODEL_DIR_ENV, str(custom_dir))

    assert ner.get_ner_model_cache_dir() == custom_dir
