"""Construct a voice stack from control-plane-resolved tenant credentials.

The worker never reads LLM/STT/TTS provider keys from process environment.  The
internal control-plane response must explicitly resolve BYOK/platform/hybrid
policy for the licensed workspace and return one credential per selected direct
provider.  This prevents an absent BYOK key from silently falling back to a
platform key present on the machine.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from livekit.plugins import silero as _SILERO_PLUGIN
except ImportError:  # pragma: no cover - missing extra is a setup error
    _SILERO_PLUGIN = None

for _plugin_name in ("openai", "elevenlabs", "deepgram", "anthropic"):
    try:
        __import__(f"livekit.plugins.{_plugin_name}")
    except ImportError:
        pass

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
_ENVELOPE_AAD_PREFIX = b"nexora-runtime-providers:v1:"


class ProviderConfigurationError(RuntimeError):
    """Safe configuration error that never includes provider credentials."""


@dataclass(frozen=True)
class ProviderStack:
    stt: object
    llm: object
    tts: object
    vad: object
    turn_detection: object


_LOCAL_VAD: object | None = None


def build_provider_stack(
    config: Mapping[str, object],
    *,
    max_completion_tokens: int | None = None,
) -> ProviderStack:
    runtime = config.get("runtimeProviders")
    if not isinstance(runtime, Mapping):
        raise ProviderConfigurationError("Tenant runtime providers are not configured")

    transport = str(runtime.get("transport") or "direct").strip().lower()
    if transport not in {"direct", "livekit_inference"}:
        raise ProviderConfigurationError("Unsupported voice provider transport")
    _validate_runtime_policy(runtime)
    if max_completion_tokens is not None and (
        isinstance(max_completion_tokens, bool) or not 1 <= max_completion_tokens <= 1_024
    ):
        raise ProviderConfigurationError("LLM output token limit is invalid")
    if transport == "livekit_inference":
        return _inference_stack(
            runtime,
            str(config.get("locale") or "auto"),
            max_completion_tokens=max_completion_tokens,
        )
    llm_source = str(_provider_config(runtime, "llm").get("credentialSource") or "").strip().lower()
    if llm_source == "platform" and max_completion_tokens is None:
        raise ProviderConfigurationError("Platform LLM token budget is unavailable")
    return _direct_stack(
        runtime,
        str(config.get("locale") or "auto"),
        max_completion_tokens=max_completion_tokens,
    )


def _uses_inference(config: Mapping[str, object]) -> bool:
    return bool(config.get("inference")) and not str(config.get("apiKey") or "").strip()


def _validate_runtime_policy(runtime: Mapping[str, object]) -> None:
    """Verify that each decrypted capability carries an explicit licensed source."""

    mode = str(runtime.get("mode") or "").strip().lower()
    if mode not in {"byok", "platform", "hybrid"}:
        raise ProviderConfigurationError("Provider mode is missing or invalid")
    transport = str(runtime.get("transport") or "direct").strip().lower()
    hybrid = runtime.get("hybridPolicy")
    for kind in ("llm", "stt", "tts"):
        config = _provider_config(runtime, kind)
        if _uses_inference(config):
            if transport != "livekit_inference":
                raise ProviderConfigurationError("LiveKit Inference is not enabled for this session")
            if str(config.get("credentialSource") or "").strip().lower() != "platform":
                raise ProviderConfigurationError(f"Licensed credential source mismatch for {kind}")
            continue
        if transport == "livekit_inference":
            raise ProviderConfigurationError("LiveKit Inference session is incomplete")
        source = str(config.get("credentialSource") or "").strip().lower()
        expected = mode
        if mode == "hybrid":
            if not isinstance(hybrid, Mapping):
                raise ProviderConfigurationError("Hybrid provider policy is incomplete")
            expected = str(hybrid.get(kind) or "").strip().lower()
        if expected not in {"byok", "platform"} or source != expected:
            raise ProviderConfigurationError(f"Licensed credential source mismatch for {kind}")


def decrypt_runtime_providers_envelope(
    envelope: object,
    room_name: str,
    *,
    encoded_key: str | None = None,
    now: int | None = None,
) -> Mapping[str, object]:
    """Decrypt a short-lived, room-bound control-plane provider envelope."""

    if not isinstance(envelope, Mapping):
        raise ProviderConfigurationError("Runtime provider envelope is unavailable")
    if envelope.get("alg") != "A256GCM" or envelope.get("v") != 1:
        raise ProviderConfigurationError("Runtime provider envelope is invalid")
    nonce_text = envelope.get("nonce")
    ciphertext_text = envelope.get("ciphertext")
    if not isinstance(nonce_text, str) or not isinstance(ciphertext_text, str):
        raise ProviderConfigurationError("Runtime provider envelope is invalid")

    key_text = encoded_key if encoded_key is not None else os.environ.get("WORKER_CONFIG_KEY", "")
    try:
        key = _decode_base64url(key_text, expected_length=32)
        nonce = _decode_base64url(nonce_text, expected_length=12)
        ciphertext = _decode_base64url(ciphertext_text, min_length=17, max_length=64 * 1024)
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            _ENVELOPE_AAD_PREFIX + room_name.encode("utf-8"),
        )
        value = json.loads(plaintext)
    except Exception as error:
        raise ProviderConfigurationError("Runtime provider envelope could not be verified") from error
    if not isinstance(value, Mapping) or value.get("roomName") != room_name:
        raise ProviderConfigurationError("Runtime provider envelope scope is invalid")

    issued_at = value.get("issuedAt")
    expires_at = value.get("expiresAt")
    current = int(time.time()) if now is None else int(now)
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at <= issued_at
        or expires_at - issued_at > 120
        or issued_at > current + 30
        or expires_at < current
    ):
        raise ProviderConfigurationError("Runtime provider envelope has expired")
    runtime = value.get("runtimeProviders")
    if not isinstance(runtime, Mapping):
        raise ProviderConfigurationError("Runtime provider envelope is incomplete")
    return runtime


def _inference_stack(
    runtime: Mapping[str, object],
    locale: str,
    *,
    max_completion_tokens: int | None,
) -> ProviderStack:
    del locale, max_completion_tokens
    from livekit.agents import inference

    stt_config = _provider_config(runtime, "stt")
    llm_config = _provider_config(runtime, "llm")
    tts_config = _provider_config(runtime, "tts")
    if not (_uses_inference(stt_config) and _uses_inference(llm_config) and _uses_inference(tts_config)):
        raise ProviderConfigurationError("LiveKit Inference session is incomplete")
    return ProviderStack(
        stt=inference.STT(model=_model(stt_config, "model", "elevenlabs/scribe_v2_realtime")),
        llm=inference.LLM(model=_model(llm_config, "model", "openai/gpt-4.1-mini")),
        tts=inference.TTS(
            model=_model(tts_config, "model", "elevenlabs/eleven_flash_v2_5"),
            voice=_model(tts_config, "voice", "Rachel"),
        ),
        vad=None,
        turn_detection=inference.TurnDetector(),
    )


def _build_direct_llm(llm_config: Mapping[str, object], *, max_completion_tokens: int | None) -> object:
    from livekit.plugins import openai

    llm_provider = _provider_name(llm_config)
    llm_key = _required_key(llm_config, "LLM")
    llm_model = _model(
        llm_config,
        "model",
        {
            "openai": "gpt-4.1-mini",
            "groq": "openai/gpt-oss-120b",
            "anthropic": "claude-sonnet-4-6",
        }.get(llm_provider, ""),
    )
    if llm_provider == "openai":
        llm_options: dict[str, object] = {
            "model": llm_model,
            "api_key": llm_key,
            "max_retries": 1,
            "store": False,
        }
        if max_completion_tokens is not None:
            llm_options["max_completion_tokens"] = max_completion_tokens
        return openai.LLM(**llm_options)
    if llm_provider == "groq":
        llm_options = {
            "model": llm_model,
            "api_key": llm_key,
            "base_url": "https://api.groq.com/openai/v1",
            "max_retries": 1,
        }
        if max_completion_tokens is not None:
            llm_options["max_completion_tokens"] = max_completion_tokens
        return openai.LLM(**llm_options)
    if llm_provider == "anthropic":
        try:
            from livekit.plugins import anthropic
        except ImportError as error:
            raise ProviderConfigurationError("Anthropic voice plugin is not installed") from error
        anthropic_options: dict[str, object] = {"model": llm_model, "api_key": llm_key}
        if max_completion_tokens is not None:
            anthropic_options["max_tokens"] = max_completion_tokens
        return anthropic.LLM(**anthropic_options)
    raise ProviderConfigurationError("Unsupported LLM provider")


def _build_direct_stt(stt_config: Mapping[str, object], locale: str) -> object:
    from livekit.plugins import elevenlabs, openai

    stt_provider = _provider_name(stt_config)
    stt_key = _required_key(stt_config, "STT")
    language = _language(stt_config.get("languageCode"), locale)
    if stt_provider == "elevenlabs":
        options: dict[str, object] = {
            "api_key": stt_key,
            "model": _model(stt_config, "model", "scribe_v2_realtime"),
            "enable_logging": False,
        }
        if language:
            options["language_code"] = language
        return elevenlabs.STT(**options)
    if stt_provider == "openai":
        return openai.STT(
            model=_model(stt_config, "model", "gpt-4o-mini-transcribe"),
            api_key=stt_key,
            language=language or "en",
            detect_language=not bool(language),
        )
    if stt_provider == "deepgram":
        try:
            from livekit.plugins import deepgram
        except ImportError as error:
            raise ProviderConfigurationError("Deepgram voice plugin is not installed") from error
        return deepgram.STT(
            model=_model(stt_config, "model", "nova-3"),
            api_key=stt_key,
            language=language or "multi",
            mip_opt_out=True,
        )
    raise ProviderConfigurationError("Unsupported STT provider")


def _build_direct_tts(tts_config: Mapping[str, object]) -> object:
    from livekit.plugins import elevenlabs, openai

    tts_provider = _provider_name(tts_config)
    tts_key = _required_key(tts_config, "TTS")
    if tts_provider == "elevenlabs":
        return elevenlabs.TTS(
            model=_model(tts_config, "model", "eleven_flash_v2_5"),
            voice_id=_model(tts_config, "voice", "21m00Tcm4TlvDq8ikWAM"),
            api_key=tts_key,
            apply_language_text_normalization=True,
            enable_logging=False,
        )
    if tts_provider == "openai":
        return openai.TTS(
            model=_model(tts_config, "model", "gpt-4o-mini-tts"),
            voice=_model(tts_config, "voice", "ash"),
            api_key=tts_key,
        )
    raise ProviderConfigurationError("Unsupported TTS provider")


def _direct_stack(
    runtime: Mapping[str, object],
    locale: str,
    *,
    max_completion_tokens: int | None,
) -> ProviderStack:
    return ProviderStack(
        stt=_build_direct_stt(_provider_config(runtime, "stt"), locale),
        llm=_build_direct_llm(_provider_config(runtime, "llm"), max_completion_tokens=max_completion_tokens),
        tts=_build_direct_tts(_provider_config(runtime, "tts")),
        vad=_local_vad(),
        turn_detection="vad",
    )


def _local_vad() -> object:
    global _LOCAL_VAD
    if _SILERO_PLUGIN is None:
        raise ProviderConfigurationError("Local Silero VAD plugin is not installed")
    if _LOCAL_VAD is None:
        _LOCAL_VAD = _SILERO_PLUGIN.VAD.load()
    return _LOCAL_VAD


def prewarm_local_vad() -> object:
    """Load the local VAD during worker-process setup, before the first call."""

    return _local_vad()


def _provider_config(runtime: Mapping[str, object], kind: str) -> Mapping[str, object]:
    value = runtime.get(kind)
    if not isinstance(value, Mapping):
        raise ProviderConfigurationError(f"Tenant {kind.upper()} provider is not configured")
    return value


def _provider_name(config: Mapping[str, object]) -> str:
    provider = str(config.get("provider") or "").strip().lower()
    if not provider or not re.fullmatch(r"[a-z0-9_-]{1,40}", provider):
        raise ProviderConfigurationError("Provider name is invalid")
    return provider


def _required_key(config: Mapping[str, object], label: str) -> str:
    # Intentionally do not call os.getenv: missing tenant credentials fail closed.
    value = config.get("apiKey")
    if not isinstance(value, str) or not 8 <= len(value.strip()) <= 512:
        raise ProviderConfigurationError(f"Resolved {label} credential is unavailable")
    return value.strip()


def _model(config: Mapping[str, object], field: str, default: str) -> str:
    raw = config.get(field)
    if raw is None and default:
        raw = default
    if not isinstance(raw, str) or (raw and not _NAME_RE.fullmatch(raw)):
        raise ProviderConfigurationError(f"Provider {field} is invalid")
    return raw


def _language(raw: object, locale: str) -> str:
    value = str(raw or "").strip()
    if not value:
        value = {
            "ar": "ar",
            "en-US": "en",
            "en-GB": "en",
            "hi-IN": "hi",
            # Let provider auto-detect Hinglish/code switching.
            "hi-en": "",
            "auto": "",
        }.get(locale, "")
    if value and not _LANGUAGE_RE.fullmatch(value):
        raise ProviderConfigurationError("Provider language is invalid")
    return value


def _decode_base64url(
    value: str,
    *,
    expected_length: int | None = None,
    min_length: int = 0,
    max_length: int = 1_024,
) -> bytes:
    if not value or len(value) > max_length * 2 or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError("invalid encoded value")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if expected_length is not None and len(raw) != expected_length:
        raise ValueError("invalid encoded length")
    if not min_length <= len(raw) <= max_length:
        raise ValueError("invalid encoded length")
    return raw
