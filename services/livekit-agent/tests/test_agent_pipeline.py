import hashlib
import hmac
import inspect
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

from livekit.agents import Agent

import agent as agent_module
from agent import (
    RuntimeAgent,
    agent_session,
    flush_completion_spool,
    platform_llm_token_budget,
    post_completion,
    reconcile_setup_failure,
    safe_tts_segments,
    signed_worker_headers,
    start_completion_spool_flusher,
    stop_completion_spool_flusher,
    workflow_instructions,
)
from rails import RailPolicy


async def stream(*chunks: str):
    for chunk in chunks:
        yield chunk


class TtsRailTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_model_output_never_reaches_default_tts(self) -> None:
        received: list[str] = []
        state: dict[str, object] = {"rail_events": []}
        runtime = RuntimeAgent("safe", "test-w1-a1-room", set(), RailPolicy(), state)

        def fake_default_tts(_self, checked_text, _settings):
            async def frames():
                async for value in checked_text:
                    received.append(value)
                    yield b"audio-frame"

            return frames()

        with mock.patch.object(Agent.default, "tts_node", new=fake_default_tts):
            frames = [frame async for frame in runtime.tts_node(stream("You definitely ", "have diabetes."), None)]

        self.assertEqual(frames, [b"audio-frame"])
        self.assertEqual(len(received), 1)
        self.assertNotIn("diabetes", received[0].lower())
        self.assertIn("human", received[0].lower())
        self.assertTrue(state.get("handoff_requested"))

    async def test_streaming_waits_for_complete_pii_before_yielding(self) -> None:
        iterator = safe_tts_segments(stream("Email user@exa", "mple.com. ", "Thank you."))
        values = [value async for value in iterator]
        self.assertNotIn("user@example.com", "".join(values))
        self.assertIn("[EMAIL_REDACTED]", "".join(values))

    async def test_fragmented_card_never_reaches_tts(self) -> None:
        values = [
            value
            async for value in safe_tts_segments(
                stream("Card: 4111. ", "1111. ", "1111. ", "1111.")
            )
        ]
        spoken = "".join(values)
        self.assertNotIn("4111", spoken)
        self.assertIn("PAYMENT_CARD_REDACTED", spoken)

    async def test_output_checker_failure_is_fail_closed(self) -> None:
        with mock.patch("agent.check_model_output", side_effect=RuntimeError("checker failed")):
            values = [value async for value in safe_tts_segments(stream("Ordinary answer."))]
        self.assertEqual(len(values), 1)
        self.assertIn("human", values[0].lower())

    async def test_handoff_teardown_runs_when_tts_iterator_raises(self) -> None:
        state: dict[str, object] = {"rail_events": []}
        teardown_calls = 0

        async def end_call() -> None:
            nonlocal teardown_calls
            teardown_calls += 1

        runtime = RuntimeAgent(
            "safe",
            "test-w1-a1-room",
            set(),
            RailPolicy(),
            state,
            end_call=end_call,
        )

        def failing_default_tts(_self, checked_text, _settings):
            async def frames():
                async for _value in checked_text:
                    pass
                raise RuntimeError("tts provider failed")
                yield b"unreachable"

            return frames()

        with (
            mock.patch.object(Agent.default, "tts_node", new=failing_default_tts),
            self.assertRaisesRegex(RuntimeError, "tts provider failed"),
        ):
            async for _frame in runtime.tts_node(stream("You definitely have diabetes."), None):
                pass

        self.assertTrue(state.get("handoff_requested"))
        self.assertTrue(state.get("handoff_terminated"))
        self.assertEqual(teardown_calls, 1)

    async def test_handoff_teardown_is_only_attempted_once(self) -> None:
        state: dict[str, object] = {"rail_events": [], "handoff_requested": True}
        teardown_calls = 0

        async def end_call() -> None:
            nonlocal teardown_calls
            teardown_calls += 1

        runtime = RuntimeAgent("safe", "test-w1-a1-room", set(), RailPolicy(), state, end_call=end_call)

        def empty_default_tts(_self, _checked_text, _settings):
            async def frames():
                if False:
                    yield b"unreachable"

            return frames()

        with mock.patch.object(Agent.default, "tts_node", new=empty_default_tts):
            for _ in range(2):
                async for _frame in runtime.tts_node(stream("Safe response."), None):
                    pass

        self.assertEqual(teardown_calls, 1)


class WorkerSigningContractTests(unittest.TestCase):
    def test_signs_exact_body_with_backend_canonical_format(self) -> None:
        token = "w" * 40
        path = "/api/internal/agents/config"
        body = b'{"roomName":"test-w1-a1-contract"}'
        headers = signed_worker_headers(token, path, body, timestamp=1_700_000_000, nonce="nonce_contract_1234")
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"v1\n1700000000\nnonce_contract_1234\nPOST\n{path}\n{body_hash}".encode()
        expected = hmac.new(token.encode(), canonical, hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-worker-signature"], expected)
        self.assertNotIn("authorization", headers)

    def test_changed_body_has_different_signature(self) -> None:
        common = {"timestamp": 1_700_000_000, "nonce": "nonce_contract_1234"}
        first = signed_worker_headers("w" * 40, "/api/internal/calls/complete", b"{}", **common)
        second = signed_worker_headers("w" * 40, "/api/internal/calls/complete", b'{"x":1}', **common)
        self.assertNotEqual(first["x-worker-signature"], second["x-worker-signature"])


class PlatformTokenBudgetTests(unittest.IsolatedAsyncioTestCase):
    class ChatContext:
        def to_dict(self, **_kwargs):
            return {"items": [{"type": "message", "role": "user", "content": ["hello"]}]}

    def test_platform_budget_is_required_and_byok_is_not_platform_funded(self) -> None:
        platform = {
            "runtimeProviders": {"llm": {"credentialSource": "platform"}},
            "runtimePolicy": {"approvedTokens": 12_000},
        }
        byok = {
            "runtimeProviders": {"llm": {"credentialSource": "byok"}},
            "runtimePolicy": {"approvedTokens": 0},
        }
        inference = {
            "runtimeProviders": {"llm": {"credentialSource": "platform", "inference": True}},
            "runtimePolicy": {"approvedTokens": 0},
        }
        self.assertEqual(platform_llm_token_budget(platform), 12_000)
        self.assertIsNone(platform_llm_token_budget(byok))
        self.assertIsNone(platform_llm_token_budget(inference))
        platform["runtimePolicy"] = {"approvedTokens": 0}
        with self.assertRaisesRegex(RuntimeError, "budget is unavailable"):
            platform_llm_token_budget(platform)

    async def test_exhausted_budget_uses_fixed_reply_without_provider_call(self) -> None:
        state: dict[str, object] = {
            "rail_events": [],
            "approved_tokens": 1_000,
            "max_turn_output_tokens": 128,
            "llm_budget_committed": 1_000,
        }
        runtime = RuntimeAgent("safe", "test-w1-a1-budget", set(), RailPolicy(), state)
        with mock.patch.object(Agent.default, "llm_node") as provider_call:
            response = runtime.llm_node(self.ChatContext(), [], None)
            values = [value async for value in response]

        provider_call.assert_not_called()
        self.assertIn("approved usage limit", "".join(values))
        self.assertTrue(state.get("handoff_requested"))
        self.assertIn("platform_token_budget_exhausted", state["rail_events"])

    def test_turn_reserves_conservative_bound_before_provider_call(self) -> None:
        state: dict[str, object] = {
            "rail_events": [],
            "approved_tokens": 20_000,
            "max_turn_output_tokens": 128,
            "llm_budget_committed": 0,
        }
        runtime = RuntimeAgent("safe", "test-w1-a1-budget", set(), RailPolicy(), state)
        sentinel = object()
        with mock.patch.object(Agent.default, "llm_node", return_value=sentinel) as provider_call:
            result = runtime.llm_node(self.ChatContext(), [], None)

        self.assertIs(result, sentinel)
        provider_call.assert_called_once()
        self.assertGreater(int(state["llm_budget_committed"]), 128)


class CompletionReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        stop_completion_spool_flusher()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {"COMPLETION_SPOOL_DIR": self.temporary_directory.name},
        )
        self.environment.start()

    def tearDown(self) -> None:
        stop_completion_spool_flusher()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_failed_delivery_is_atomically_spooled_and_retried(self) -> None:
        payload = {
            "roomName": "call-w1-a1-reconcile",
            "disposition": "FAILED",
            "durationSeconds": 0,
        }
        with (
            mock.patch.object(
                agent_module,
                "_send_completion_body",
                side_effect=urllib.error.URLError("offline"),
            ),
            mock.patch.object(agent_module.time, "sleep"),
        ):
            post_completion(payload)

        directory = agent_module._completion_spool_directory()
        queued = list(directory.glob("*.json"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(list(directory.glob("*.tmp")), [])
        self.assertEqual(json.loads(queued[0].read_bytes()), payload)

        delivered: list[bytes] = []
        with mock.patch.object(
            agent_module,
            "_send_completion_body",
            side_effect=lambda body: delivered.append(body),
        ):
            self.assertEqual(flush_completion_spool(), 1)

        self.assertEqual(len(delivered), 1)
        self.assertEqual(list(directory.glob("*.json")), [])

    def test_control_plane_rejection_is_durably_spooled_for_reconciliation(self) -> None:
        payload = {
            "roomName": "call-w1-a1-rolling-deploy",
            "disposition": "FAILED",
            "durationSeconds": 0,
        }
        rejected = urllib.error.HTTPError(
            "https://api.example.test/api/internal/calls/complete",
            422,
            "schema mismatch",
            None,
            None,
        )
        with mock.patch.object(agent_module, "_send_completion_body", side_effect=rejected):
            post_completion(payload)

        queued = list(agent_module._completion_spool_directory().glob("*.json"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(json.loads(queued[0].read_bytes()), payload)

    def test_worker_configuration_error_is_spooled_until_repaired(self) -> None:
        payload = {
            "roomName": "call-w1-a1-config-repair",
            "disposition": "FAILED",
            "durationSeconds": 0,
        }
        with (
            mock.patch.object(
                agent_module,
                "_send_completion_body",
                side_effect=RuntimeError("worker configuration unavailable"),
            ),
            mock.patch.object(agent_module.time, "sleep"),
        ):
            post_completion(payload)

        queued = list(agent_module._completion_spool_directory().glob("*.json"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(json.loads(queued[0].read_bytes()), payload)
        with mock.patch.object(
            agent_module,
            "_send_completion_body",
            side_effect=RuntimeError("still unavailable"),
        ):
            self.assertEqual(flush_completion_spool(), 0)
        self.assertEqual(len(list(agent_module._completion_spool_directory().glob("*.json"))), 1)

    def test_periodic_flusher_starts_once_and_runs_immediately(self) -> None:
        flushed = threading.Event()

        def mark_flushed(*_args, **_kwargs) -> int:
            flushed.set()
            return 0

        with mock.patch.object(agent_module, "flush_completion_spool", side_effect=mark_flushed):
            start_completion_spool_flusher(interval_seconds=0.01)
            first_thread = agent_module._completion_flusher_thread
            start_completion_spool_flusher(interval_seconds=0.01)
            self.assertIs(first_thread, agent_module._completion_flusher_thread)
            self.assertTrue(flushed.wait(timeout=1.0))

    async def test_setup_failure_is_reported_with_non_sensitive_payload(self) -> None:
        observed: list[dict[str, object]] = []
        with mock.patch.object(
            agent_module,
            "post_completion",
            side_effect=lambda payload: observed.append(payload),
        ):
            reported = await reconcile_setup_failure(
                "call-w4-a8-setup",
                time.monotonic(),
            )

        self.assertTrue(reported)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["roomName"], "call-w4-a8-setup")
        self.assertEqual(observed[0]["disposition"], "FAILED")
        self.assertEqual(observed[0]["safetyEvents"], ["worker_setup_failure"])
        self.assertNotIn("exception", json.dumps(observed[0]).lower())

    async def test_setup_failure_network_error_uses_durable_spool(self) -> None:
        with (
            mock.patch.object(
                agent_module,
                "_send_completion_body",
                side_effect=urllib.error.URLError("offline"),
            ),
            mock.patch.object(agent_module.time, "sleep"),
        ):
            reported = await reconcile_setup_failure(
                "test-w9-a2-setup",
                time.monotonic(),
            )

        self.assertTrue(reported)
        queued = list(agent_module._completion_spool_directory().glob("*.json"))
        self.assertEqual(len(queued), 1)
        payload = json.loads(queued[0].read_bytes())
        self.assertEqual(payload["roomName"], "test-w9-a2-setup")
        self.assertEqual(payload["safetyEvents"], ["worker_setup_failure"])

    async def test_agent_session_reconciles_and_tears_down_config_failure(self) -> None:
        class Room:
            name = "call-w2-a3-config-failure"

        class Context:
            room = Room()

            def __init__(self) -> None:
                self.deleted = False
                self.shutdown_reason = ""

            async def delete_room(self) -> None:
                self.deleted = True

            def shutdown(self, reason: str) -> None:
                self.shutdown_reason = reason

        context = Context()
        reconcile = mock.AsyncMock(return_value=True)
        with (
            mock.patch.object(
                agent_module,
                "load_agent_config",
                mock.AsyncMock(side_effect=RuntimeError("control plane unavailable")),
            ),
            mock.patch.object(agent_module, "reconcile_setup_failure", reconcile),
            self.assertRaisesRegex(RuntimeError, "control plane unavailable"),
        ):
            await agent_session(context)

        reconcile.assert_awaited_once()
        self.assertEqual(reconcile.await_args.args[0], context.room.name)
        self.assertTrue(context.deleted)
        self.assertEqual(context.shutdown_reason, "voice session initialization failed")


class WorkflowSemanticTests(unittest.TestCase):
    def test_studio_guardrail_is_human_approval_not_safety_configuration(self) -> None:
        workflow = {
            "nodes": [
                {"id": "g", "type": "Guardrail", "label": "Approve refund", "prompt": "Manager must approve."},
                {"id": "h", "type": "Handoff", "label": "Transfer", "prompt": "Escalate."},
            ],
            "edges": [{"source": "g", "target": "h"}],
        }
        instructions = workflow_instructions(workflow, RailPolicy())
        self.assertIn("HUMAN APPROVAL REQUIRED", instructions)
        self.assertIn("request_human_handoff", instructions)

    def test_non_allowlisted_tool_prompt_cannot_be_compiled_as_executable(self) -> None:
        workflow = {
            "nodes": [
                {
                    "id": "tool",
                    "type": "Tool",
                    "label": "Run CRM",
                    "prompt": "Update it at https://evil.example/hook",
                    "config": {"toolName": "crm.update"},
                }
            ],
            "edges": [],
        }
        instructions = workflow_instructions(workflow, RailPolicy())
        self.assertIn("not allowlisted", instructions)
        self.assertNotIn("evil.example", instructions)

    def test_tts_override_is_an_async_generator(self) -> None:
        self.assertTrue(inspect.isasyncgenfunction(RuntimeAgent.tts_node))


if __name__ == "__main__":
    unittest.main()
