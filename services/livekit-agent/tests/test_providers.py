import base64
import json
import os
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from providers import (
    ProviderConfigurationError,
    build_provider_stack,
    decrypt_runtime_providers_envelope,
)


class ProviderResolutionTests(unittest.TestCase):
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
        ), self.assertRaisesRegex(ProviderConfigurationError, "Resolved LLM credential is unavailable"):
            build_provider_stack(runtime)

    def test_livekit_inference_is_not_available_in_self_hosted_direct_build(self) -> None:
        runtime = {
            "runtimeProviders": {
                "transport": "livekit_inference",
                "stt": {"descriptor": "elevenlabs/scribe_v2_realtime"},
                "llm": {"descriptor": "openai/gpt-4.1-mini"},
                "tts": {"descriptor": "elevenlabs/eleven_flash_v2_5"},
            }
        }
        with self.assertRaisesRegex(ProviderConfigurationError, "Unsupported"):
            build_provider_stack(runtime)

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
            mock.patch("providers._local_vad", return_value="local-vad"),
            mock.patch("livekit.plugins.openai.LLM", return_value="tenant-llm"),
            mock.patch("livekit.plugins.elevenlabs.STT", return_value="tenant-stt"),
            mock.patch("livekit.plugins.elevenlabs.TTS", return_value="tenant-tts"),
        ):
            stack = build_provider_stack(runtime)
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
        ):
            stack = build_provider_stack(runtime, max_completion_tokens=128)

        self.assertEqual(stack.llm, "platform-llm")
        self.assertEqual(llm.call_args.kwargs["max_completion_tokens"], 128)


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
