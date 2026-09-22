import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from human_simulator import (
    mono_s16,
    pcm_chunks,
    room_is_waiting_for_staff,
    should_join_as_staff,
    synthesize_wav,
)


class HumanSimulatorTests(unittest.TestCase):
    def test_waits_until_only_the_caller_remains_in_a_test_room(self) -> None:
        metadata = '{"handoff":"waiting_for_staff"}'
        self.assertFalse(room_is_waiting_for_staff("test-w1-a1-room", metadata, 2))
        self.assertTrue(room_is_waiting_for_staff("test-w1-a1-room", metadata, 1))
        self.assertFalse(room_is_waiting_for_staff("call-w1-a1-room", metadata, 1))
        self.assertFalse(room_is_waiting_for_staff("test-w1-a1-room", "{}", 1))
        self.assertFalse(room_is_waiting_for_staff("test-w1-a1-room", "not-json", 1))

    def test_force_closed_agent_lets_staff_join_the_caller(self) -> None:
        common = {
            "participant_count": 1,
            "has_agent": False,
            "has_staff": False,
            "room_age_seconds": 2.0,
        }
        self.assertFalse(
            should_join_as_staff("test-w1-a1-room", "", agent_was_seen=False, **common)
        )
        self.assertTrue(
            should_join_as_staff("test-w1-a1-room", "", agent_was_seen=True, **common)
        )
        self.assertTrue(
            should_join_as_staff(
                "test-w1-a1-room",
                "",
                participant_count=1,
                has_agent=False,
                has_staff=False,
                agent_was_seen=False,
                room_age_seconds=8.0,
            )
        )
        self.assertFalse(
            should_join_as_staff(
                "test-w1-a1-room",
                "",
                participant_count=2,
                has_agent=True,
                has_staff=False,
                agent_was_seen=True,
                room_age_seconds=30.0,
            )
        )

    def test_pcm_chunks_are_20ms_mono_frames(self) -> None:
        pcm = b"\x01\x00" * 16000
        frames = pcm_chunks(pcm, 16000)
        self.assertEqual(frames[0][1], 320)
        self.assertEqual(len(frames[0][0]), 640)
        self.assertEqual(sum(len(chunk) for chunk, _samples in frames), len(pcm))

    def test_stereo_wav_becomes_mono(self) -> None:
        stereo = b"\x01\x00\x02\x00" * 4
        mono = mono_s16(stereo, 2, 2)
        self.assertEqual(mono, b"\x01\x00" * 4)

    def test_linux_tts_uses_espeak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "line.wav"

            def fake_run(args, **_kwargs):
                destination.write_bytes(b"RIFF" + b"\x00" * 12)
                return None

            with (
                patch("human_simulator.sys.platform", "linux"),
                patch("human_simulator._linux_tts_binary", return_value="/usr/bin/espeak-ng"),
                patch("human_simulator.subprocess.run", side_effect=fake_run) as run,
            ):
                synthesize_wav("hello staff", destination)
            self.assertEqual(run.call_args.args[0][:3], ["/usr/bin/espeak-ng", "-w", str(destination)])
            self.assertEqual(run.call_args.args[0][-1], "hello staff")
            self.assertTrue(destination.is_file())

    def test_unsupported_platform_raises(self) -> None:
        with (
            patch("human_simulator.sys.platform", "darwin"),
            tempfile.TemporaryDirectory() as directory,
        ):
            with self.assertRaises(RuntimeError):
                synthesize_wav("hello", Path(directory) / "line.wav")


if __name__ == "__main__":
    unittest.main()
