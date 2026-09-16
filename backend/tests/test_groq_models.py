from app.provider_vault import GROQ_DEFAULT_MODEL, resolve_groq_model


def test_resolve_groq_model_maps_retired_llama_ids() -> None:
    assert resolve_groq_model("gpt-4.1-mini") == GROQ_DEFAULT_MODEL
    assert resolve_groq_model("llama-3.3-70b-versatile") == "openai/gpt-oss-120b"
    assert resolve_groq_model("llama-3.1-8b-instant") == "openai/gpt-oss-20b"


def test_resolve_groq_model_keeps_current_groq_ids() -> None:
    assert resolve_groq_model("openai/gpt-oss-20b") == "openai/gpt-oss-20b"
    assert resolve_groq_model("qwen/qwen3.6-27b", "openai/gpt-oss-120b") == "qwen/qwen3.6-27b"
