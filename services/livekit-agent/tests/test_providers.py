import base64
import json
import os
import sys
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from providers import (
    ProviderConfigurationError,
    _SILERO_PLUGIN,
    build_provider_stack,
    decrypt_runtime_providers_envelope,
    prewarm_audio_resampler,
)


def _passthrough_stream_adapter(**kwargs):
    return kwargs["tts"]


class ProviderResolutionTests(unittest.TestCase):
    def test_silero_plugin_is_registered_when_providers_is_imported(self) -> None:
        self.assertIsNotNone(_SILERO_PLUGIN)

    def test_missing_resolved_key_never_falls_back_to_process_environment(self) -> None:
        runtime = {
            "runtimeProviders": {
                "transport": "direct",
                "mode": "byok",
                "llm": {"provider": "openai", "model": "gpt-4.1-mini", "credentialSource": "byok"},
                "stt": {"provider": "elevenlabs", "model": "scribe_v2_realtime", "credentialSource": "byok"},
                "tts": {"provider": "elevenlabs", "model": "eleven_flash_v2_5", "credentialSource": "byok"},
            }
        }
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "platform-openai-secret", "ELEVEN_API_KEY": "platform-eleven-secret"},
            clear=False,
        ), self.assertRaisesRegex(ProviderConfigurationError, "Resolved (LLM|STT|TTS) credential is unavailable"):
            build_provider_stack(runtime)

    def test_livekit_inference_uses_envelope_not_process_environment(self) -> None:
        runtime = {
            "runtimeProviders": {
                "transport": "livekit_inference",
                "mode": "hybrid",
                "hybridPolicy": {
                    "llm": "byok",
                    "stt": "byok",
                    "tts": "byok",
                    "realtime": "platform",
                },
                "llm": {
                    "provider": "livekit",
                    "model": "openai/gpt-4.1-mini",
                    "credentialSource": "platform",
                    "inference": True,
                },
                "stt": {
                    "provider": "livekit",
                    "model": "elevenlabs/scribe_v2_realtime",
                    "credentialSource": "platform",
                    "inference": True,
                },
                "tts": {
                    "provider": "livekit",
                    "model": "elevenlabs/eleven_flash_v2_5",
                    "voice": "Rachel",
                    "credentialSource": "platform",
                    "inference": True,
                },
            }
        }
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "platform-openai-secret", "ELEVEN_API_KEY": "platform-eleven-secret"},
            clear=False,
        ), mock.patch("providers._local_vad", return_value="local-vad") as vad, mock.patch(
            "livekit.agents.inference.LLM", return_value="inference-llm"
        ) as llm, mock.patch(
            "livekit.agents.inference.STT", return_value="inference-stt"
        ) as stt, mock.patch(
            "livekit.agents.inference.TTS", return_value="inference-tts"
        ) as tts, mock.patch(
            "livekit.agents.inference.TurnDetector", return_value="inference-turn"
        ):
            stack = build_provider_stack(runtime)
        self.assertEqual((stack.llm, stack.stt, stack.tts), ("inference-llm", "inference-stt", "inference-tts"))
        self.assertIsNone(stack.vad)
        self.assertEqual(stack.turn_detection, "inference-turn")
        vad.assert_not_called()
        self.assertIn("openai/gpt-4.1-mini", str(llm.call_args))
        self.assertNotIn("platform-openai-secret", str(stt.call_args))
        self.assertNotIn("platform-eleven-secret", str(tts.call_args))
        self.assertNotIn("api_key", str(llm.call_args))

    def test_unknown_transport_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProviderConfigurationError, "Unsupported"):
            build_provider_stack({"runtimeProviders": {"transport": "automatic"}})

    def test_provider_source_mismatch_fails_closed(self) -> None:
        runtime = {
            "runtimeProviders": {
                "transport": "direct",
                "mode": "byok",
                "llm": {"provider": "openai", "apiKey": "tenant-openai-key", "credentialSource": "platform"},
                "stt": {"provider": "elevenlabs", "apiKey": "tenant-eleven-key", "credentialSource": "byok"},
                "tts": {"provider": "elevenlabs", "apiKey": "tenant-eleven-key", "credentialSource": "byok"},
            }
        }
        with self.assertRaisesRegex(ProviderConfigurationError, "source mismatch"):
            build_provider_stack(runtime)

    def test_direct_stack_uses_only_explicit_resolved_credentials(self) -> None:
        runtime = {
            "locale": "hi-en",
            "runtimeProviders": {
                "transport": "direct",
                "mode": "byok",
                "llm": {"provider": "openai", "model": "gpt-4.1-mini", "apiKey": "tenant-openai-key", "credentialSource": "byok"},
                "stt": {"provider": "elevenlabs", "model": "scribe_v2_realtime", "apiKey": "tenant-eleven-key", "credentialSource": "byok"},
                "tts": {
                    "provider": "elevenlabs",
                    "model": "eleven_flash_v2_5",
                    "voice": "21m00Tcm4TlvDq8ikWAM",
                    "apiKey": "tenant-eleven-key",
                    "credentialSource": "byok",
                },
            },
        }
        with (
            mock.patch("providers._local_vad", return_value="local-vad") as vad,
            mock.patch("livekit.plugins.openai.LLM", return_value="tenant-llm"),
            mock.patch("livekit.plugins.elevenlabs.STT", return_value="tenant-stt"),
            mock.patch("livekit.plugins.elevenlabs.TTS", return_value="tenant-tts"),
            mock.patch("livekit.agents.tts.StreamAdapter", side_effect=_passthrough_stream_adapter),
        ):
            stack = build_provider_stack(runtime)
        if sys.platform == "win32":
            self.assertIsNone(stack.vad)
            self.assertEqual(stack.turn_detection, "stt")
            vad.assert_not_called()
        else:
            self.assertEqual(stack.vad, "local-vad")
            self.assertEqual(stack.turn_detection, "vad")
        self.assertEqual((stack.llm, stack.stt, stack.tts), ("tenant-llm", "tenant-stt", "tenant-tts"))

    def test_platform_llm_requires_and_receives_hard_output_limit(self) -> None:
        runtime = {
            "locale": "en-US",
            "runtimeProviders": {
                "transport": "direct",
                "mode": "platform",
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "apiKey": "platform-openai-key",
                    "credentialSource": "platform",
                },
                "stt": {
                    "provider": "elevenlabs",
                    "model": "scribe_v2_realtime",
                    "apiKey": "platform-eleven-key",
                    "credentialSource": "platform",
                },
                "tts": {
                    "provider": "elevenlabs",
                    "model": "eleven_flash_v2_5",
                    "voice": "21m00Tcm4TlvDq8ikWAM",
                    "apiKey": "platform-eleven-key",
                    "credentialSource": "platform",
                },
            },
        }
        with self.assertRaisesRegex(ProviderConfigurationError, "token budget is unavailable"):
            build_provider_stack(runtime)

        with (
            mock.patch("providers._local_vad", return_value="local-vad"),
            mock.patch("livekit.plugins.openai.LLM", return_value="platform-llm") as llm,
            mock.patch("livekit.plugins.elevenlabs.STT", return_value="platform-stt"),
            mock.patch("livekit.plugins.elevenlabs.TTS", return_value="platform-tts"),
            mock.patch("livekit.agents.tts.StreamAdapter", side_effect=_passthrough_stream_adapter),
        ):
            stack = build_provider_stack(runtime, max_completion_tokens=128)

        self.assertEqual(stack.llm, "platform-llm")
        self.assertEqual(llm.call_args.kwargs["max_completion_tokens"], 128)

    def test_elevenlabs_tts_omits_enterprise_only_flags_and_maps_named_voices(self) -> None:
        runtime = {
            "locale": "en-US",
            "runtimeProviders": {
                "transport": "direct",
                "mode": "byok",
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "apiKey": "tenant-openai-key",
                    "credentialSource": "byok",
                },
                "stt": {
                    "provider": "elevenlabs",
                    "model": "scribe_v2_realtime",
                    "apiKey": "tenant-eleven-key",
                    "credentialSource": "byok",
                },
                "tts": {
                    "provider": "elevenlabs",
                    "model": "eleven_flash_v2_5",
                    "voice": "Rachel",
                    "apiKey": "tenant-eleven-key",
                    "credentialSource": "byok",
                },
            },
        }
        with (
            mock.patch("providers._local_vad", return_value="local-vad"),
            mock.patch("livekit.plugins.openai.LLM", return_value="tenant-llm"),
            mock.patch("livekit.plugins.elevenlabs.STT", return_value="tenant-stt"),
            mock.patch("livekit.plugins.elevenlabs.TTS", return_value="tenant-tts") as tts,
            mock.patch("livekit.agents.tts.StreamAdapter", side_effect=_passthrough_stream_adapter) as adapter,
        ):
            build_provider_stack(runtime)
        kwargs = tts.call_args.kwargs
        self.assertEqual(kwargs["voice_id"], "21m00Tcm4TlvDq8ikWAM")
        self.assertEqual(kwargs["model"], "eleven_flash_v2_5")
        self.assertNotIn("apply_language_text_normalization", kwargs)
        self.assertNotEqual(kwargs.get("enable_logging"), False)
        adapter.assert_called_once()

    def test_deepgram_tts_maps_named_voices_and_uses_tenant_key(self) -> None:
        runtime = {
            "locale": "en-US",
            "runtimeProviders": {
                "transport": "direct",
                "mode": "byok",
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "apiKey": "tenant-openai-key",
                    "credentialSource": "byok",
                },
                "stt": {
                    "provider": "deepgram",
                    "model": "nova-3",
                    "apiKey": "tenant-deepgram-key",
                    "credentialSource": "byok",
                },
                "tts": {
                    "provider": "deepgram",
                    "model": "aura-2-andromeda-en",
                    "voice": "thalia",
                    "apiKey": "tenant-deepgram-tts-key",
                    "credentialSource": "byok",
                },
            },
        }
        with (
            mock.patch("providers._local_vad", return_value="local-vad"),
            mock.patch("livekit.plugins.openai.LLM", return_value="tenant-llm"),
            mock.patch("livekit.plugins.deepgram.STT", return_value="tenant-stt"),
            mock.patch("livekit.plugins.deepgram.TTS", return_value="tenant-tts") as tts,
        ):
            stack = build_provider_stack(runtime)
        self.assertEqual(stack.tts, "tenant-tts")
        kwargs = tts.call_args.kwargs
        self.assertEqual(kwargs["model"], "aura-2-thalia-en")
        self.assertEqual(kwargs["api_key"], "tenant-deepgram-tts-key")
        self.assertTrue(kwargs["mip_opt_out"])

    def test_windows_direct_stack_uses_stt_turn_detection(self) -> None:
        runtime = {
            "locale": "en-US",
            "runtimeProviders": {
                "transport": "direct",
                "mode": "byok",
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "apiKey": "tenant-openai-key",
                    "credentialSource": "byok",
                },
                "stt": {
                    "provider": "elevenlabs",
                    "model": "scribe_v2_realtime",
                    "apiKey": "tenant-eleven-key",
                    "credentialSource": "byok",
                },
                "tts": {
                    "provider": "elevenlabs",
                    "model": "eleven_flash_v2_5",
                    "voice": "21m00Tcm4TlvDq8ikWAM",
                    "apiKey": "tenant-eleven-key",
                    "credentialSource": "byok",
                },
            },
        }
        with (
            mock.patch("providers.sys.platform", "win32"),
            mock.patch("providers._local_vad", return_value="local-vad") as vad,
            mock.patch("livekit.plugins.openai.LLM", return_value="tenant-llm"),
            mock.patch("livekit.plugins.elevenlabs.STT", return_value="tenant-stt"),
            mock.patch("livekit.plugins.elevenlabs.TTS", return_value="tenant-tts"),
            mock.patch("livekit.agents.tts.StreamAdapter", side_effect=_passthrough_stream_adapter),
        ):
            stack = build_provider_stack(runtime)
        self.assertIsNone(stack.vad)
        self.assertEqual(stack.turn_detection, "stt")
        vad.assert_not_called()

    def test_audio_resampler_prewarm_pushes_common_rate_pairs(self) -> None:
        resampler = mock.Mock()
        with (
            mock.patch("livekit.rtc.AudioFrame.create", return_value="silence") as create,
            mock.patch("livekit.rtc.AudioResampler", return_value=resampler) as ctor,
        ):
            prewarm_audio_resampler()
        self.assertGreaterEqual(ctor.call_count, 2)
        self.assertEqual(create.call_count, ctor.call_count)
        self.assertEqual(resampler.push.call_count, ctor.call_count)
        self.assertEqual(resampler.flush.call_count, ctor.call_count)


class ProviderEnvelopeTests(unittest.TestCase):
    room = "test-w1-a2-secure-room"
    key = bytes(range(32))

    def envelope(self, *, room: str | None = None, issued: int = 1_000, expires: int = 1_120) -> dict[str, object]:
        scoped_room = room or self.room
        plaintext = json.dumps(
            {
                "roomName": scoped_room,
                "issuedAt": issued,
                "expiresAt": expires,
                "runtimeProviders": {
                    "transport": "direct",
                    "mode": "byok",
                    "llm": {"provider": "openai", "apiKey": "tenant-secret-key", "credentialSource": "byok"},
                },
            },
            separators=(",", ":"),
        ).encode()
        nonce = bytes(range(12))
        ciphertext = AESGCM(self.key).encrypt(
            nonce,
            plaintext,
            b"nexora-runtime-providers:v1:" + scoped_room.encode(),
        )
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        return {"v": 1, "alg": "A256GCM", "nonce": encode(nonce), "ciphertext": encode(ciphertext)}

    def encoded_key(self) -> str:
        return base64.urlsafe_b64encode(self.key).decode().rstrip("=")

    def test_valid_room_bound_two_minute_envelope_decrypts(self) -> None:
        value = decrypt_runtime_providers_envelope(
            self.envelope(),
            self.room,
            encoded_key=self.encoded_key(),
            now=1_060,
        )
        self.assertEqual(value["transport"], "direct")

    def test_envelope_cannot_be_replayed_into_another_room(self) -> None:
        with self.assertRaisesRegex(ProviderConfigurationError, "could not be verified"):
            decrypt_runtime_providers_envelope(
                self.envelope(),
                "test-w9-a9-other-room",
                encoded_key=self.encoded_key(),
                now=1_060,
            )

    def test_expired_or_overlong_envelope_fails_closed(self) -> None:
        for envelope, now in ((self.envelope(), 1_121), (self.envelope(expires=1_121), 1_060)):
            with self.subTest(now=now), self.assertRaisesRegex(ProviderConfigurationError, "expired"):
                decrypt_runtime_providers_envelope(
                    envelope,
                    self.room,
                    encoded_key=self.encoded_key(),
                    now=now,
                )

    def test_wrong_worker_key_fails_without_leaking_ciphertext(self) -> None:
        wrong = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
        with self.assertRaisesRegex(ProviderConfigurationError, "could not be verified") as caught:
            decrypt_runtime_providers_envelope(self.envelope(), self.room, encoded_key=wrong, now=1_060)
        self.assertNotIn(str(self.envelope()["ciphertext"]), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
