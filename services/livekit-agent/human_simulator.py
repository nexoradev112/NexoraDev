"""Join a test room as staff after the voice agent hands the call off.

Run this beside ``python agent.py dev``. When a browser test call reaches a
human handoff, the agent says the transfer line and leaves the room. This
process then connects as the ``staff`` participant. Each line typed here is
spoken into the room and sent as a text packet the test drawer can show.

    python human_simulator.py

``/quit`` leaves the current room and waits for the next handoff.
"""

from __future__ import annotations

import array
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env.local", override=False)
load_dotenv(".env", override=False)

STAFF_IDENTITY = "staff"
STAFF_NAME = "Staff"
WAITING_METADATA = {"handoff": "waiting_for_staff"}
# A fresh test room is caller-only until the worker joins. After this grace,
# a still-empty agent slot means the worker left or was killed.
AGENT_ABSENT_GRACE_SECONDS = 8.0


def room_is_waiting_for_staff(name: str, metadata: str, participants: int) -> bool:
    """True only after the agent has left a test room that asked for a person."""

    if participants != 1 or not name.startswith("test-"):
        return False
    try:
        payload = json.loads(metadata or "")
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("handoff") == WAITING_METADATA["handoff"]


def participant_is_agent(identity: str, kind: object = 0, attribute_keys: tuple[str, ...] = ()) -> bool:
    kind_text = str(getattr(kind, "name", kind)).upper()
    if kind_text in {"4", "AGENT", "PARTICIPANT_KIND_AGENT"}:
        return True
    if identity.lower().startswith("agent"):
        return True
    return any(key.startswith("lk.agent") for key in attribute_keys)


def room_age_seconds(creation_time: int, now: float) -> float:
    if creation_time <= 0:
        return 0.0
    created = creation_time / 1000 if creation_time > 10**12 else float(creation_time)
    return max(0.0, now - created)


def should_join_as_staff(
    name: str,
    metadata: str,
    *,
    participant_count: int,
    has_agent: bool,
    has_staff: bool,
    agent_was_seen: bool,
    room_age_seconds: float,
) -> bool:
    """Join once the caller is alone, including after the worker is force-closed."""

    if has_staff or has_agent or participant_count != 1 or not name.startswith("test-"):
        return False
    if agent_was_seen or room_is_waiting_for_staff(name, metadata, participant_count):
        return True
    return room_age_seconds >= AGENT_ABSENT_GRACE_SECONDS


def mono_s16(pcm: bytes, channels: int, sampwidth: int) -> bytes:
    if sampwidth != 2 or channels < 1:
        raise ValueError("staff speech wav must be 16-bit")
    frame_width = 2 * channels
    pcm = pcm[: len(pcm) - (len(pcm) % frame_width)]
    if channels == 1:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    mono = array.array("h", (samples[index] for index in range(0, len(samples), channels)))
    if sys.byteorder != "little":
        mono.byteswap()
    return mono.tobytes()


def pcm_chunks(pcm: bytes, sample_rate: int, frame_ms: int = 20) -> list[tuple[bytes, int]]:
    samples = max(1, sample_rate * frame_ms // 1000)
    frame_bytes = samples * 2
    if len(pcm) % 2:
        pcm += b"\x00"
    frames: list[tuple[bytes, int]] = []
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += bytes(frame_bytes - len(chunk))
        frames.append((chunk, samples))
    return frames


def _linux_tts_binary() -> str | None:
    for name in ("espeak-ng", "espeak"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _synthesize_wav_windows(text: str, destination: Path) -> None:
    """Write spoken audio with the Windows speech API."""

    script_path = destination.with_suffix(".ps1")
    text_path = destination.with_suffix(".txt")
    text_path.write_text(text, encoding="utf-8")
    script_path.write_text(
        "\n".join(
            [
                "Add-Type -AssemblyName System.Speech",
                "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
                "$synth.SetOutputToWaveFile($env:STAFF_WAV)",
                "$synth.Speak((Get-Content -Raw -Encoding utf8 $env:STAFF_TXT))",
                "$synth.Dispose()",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["STAFF_WAV"] = str(destination)
    env["STAFF_TXT"] = str(text_path)
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("powershell is required for staff TTS on Windows")
    try:
        subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
            check=True,
            env=env,
            timeout=30,
            capture_output=True,
        )
    finally:
        text_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)


def _synthesize_wav_linux(text: str, destination: Path) -> None:
    """Write spoken audio with espeak-ng or espeak."""

    binary = _linux_tts_binary()
    if not binary:
        raise RuntimeError(
            "Staff TTS on Linux needs espeak-ng or espeak. "
            "Install one of them, for example: sudo apt-get install -y espeak-ng"
        )
    subprocess.run(
        [binary, "-w", str(destination), "--", text],
        check=True,
        timeout=30,
        capture_output=True,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"{binary} did not write a WAV file at {destination}")


def synthesize_wav(text: str, destination: Path) -> None:
    """Write spoken audio for staff lines on Windows or Linux."""

    if sys.platform.startswith("win"):
        _synthesize_wav_windows(text, destination)
        return
    if sys.platform.startswith("linux"):
        _synthesize_wav_linux(text, destination)
        return
    raise RuntimeError(
        f"Staff TTS is not implemented on this platform ({sys.platform}). "
        "Use Windows (SAPI) or Linux (espeak-ng/espeak)."
    )


class StaffSpeaker:
    def __init__(self, room) -> None:
        self._room = room
        self._source = None
        self._sample_rate = 0

    async def speak(self, text: str) -> None:
        from livekit import rtc

        payload = text.encode("utf-8")
        await self._room.local_participant.publish_data(payload, reliable=True, topic="staff")
        with tempfile.TemporaryDirectory(prefix="staff-tts-") as directory:
            wav_path = Path(directory) / "line.wav"
            await asyncio.to_thread(synthesize_wav, text, wav_path)
            with wave.open(str(wav_path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_rate = wav_file.getframerate()
                pcm = mono_s16(wav_file.readframes(wav_file.getnframes()), channels, wav_file.getsampwidth())
        if self._source is None or self._sample_rate != sample_rate:
            self._sample_rate = sample_rate
            self._source = rtc.AudioSource(sample_rate, 1, queue_size_ms=20_000)
            track = rtc.LocalAudioTrack.create_audio_track("staff-voice", self._source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            await self._room.local_participant.publish_track(track, options)
        for chunk, samples in pcm_chunks(pcm, sample_rate):
            await self._source.capture_frame(rtc.AudioFrame(chunk, sample_rate, 1, samples))
        await self._source.wait_for_playout()


def _livekit_credentials() -> tuple[str, str, str]:
    url = os.getenv("LIVEKIT_URL", "").strip()
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not url or not api_key or not api_secret:
        raise SystemExit("LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are required")
    return url, api_key, api_secret


async def _room_presence(lkapi, room_name: str) -> tuple[int, bool, bool]:
    from livekit import api as livekit_api

    listed = await lkapi.room.list_participants(livekit_api.ListParticipantsRequest(room=room_name))
    has_agent = False
    has_staff = False
    for participant in listed.participants:
        identity = str(getattr(participant, "identity", "") or "")
        name = str(getattr(participant, "name", "") or "")
        attributes = getattr(participant, "attributes", {}) or {}
        if identity == STAFF_IDENTITY or name == STAFF_NAME:
            has_staff = True
        if participant_is_agent(identity, getattr(participant, "kind", 0), tuple(attributes)):
            has_agent = True
    return len(listed.participants), has_agent, has_staff


async def _drop_agent_dispatches(lkapi, room_name: str) -> None:
    try:
        dispatches = await lkapi.agent_dispatch.list_dispatch(room_name)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not list agent dispatches: {exc}", file=sys.stderr)
        return
    for item in dispatches:
        dispatch_id = str(getattr(item, "id", "") or "")
        if not dispatch_id:
            continue
        try:
            await lkapi.agent_dispatch.delete_dispatch(dispatch_id, room_name)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not remove agent dispatch {dispatch_id}: {exc}", file=sys.stderr)


async def _wait_for_handoff(lkapi):
    from livekit import api as livekit_api

    seen_agents: set[str] = set()
    while True:
        listed = await lkapi.room.list_rooms(livekit_api.ListRoomsRequest())
        now = time.time()
        for room in listed.rooms:
            if not str(room.name).startswith("test-"):
                continue
            try:
                count, has_agent, has_staff = await _room_presence(lkapi, room.name)
            except Exception as exc:  # noqa: BLE001
                print(f"Could not list participants in {room.name}: {exc}", file=sys.stderr)
                continue
            if has_agent:
                seen_agents.add(room.name)
            if should_join_as_staff(
                room.name,
                room.metadata or "",
                participant_count=count,
                has_agent=has_agent,
                has_staff=has_staff,
                agent_was_seen=room.name in seen_agents,
                room_age_seconds=room_age_seconds(int(getattr(room, "creation_time", 0) or 0), now),
            ):
                seen_agents.discard(room.name)
                await _drop_agent_dispatches(lkapi, room.name)
                return room.name
        await asyncio.sleep(0.5)


async def _connect_staff(url: str, api_key: str, api_secret: str, room_name: str):
    from datetime import timedelta

    from livekit import api as livekit_api
    from livekit import rtc

    token = (
        livekit_api.AccessToken(api_key, api_secret)
        .with_identity(STAFF_IDENTITY)
        .with_name(STAFF_NAME)
        .with_ttl(timedelta(minutes=30))
        .with_grants(
            livekit_api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    room = rtc.Room()
    await room.connect(url, token)
    return room


async def _mark_room(lkapi, room_name: str, state: str) -> None:
    from livekit import api as livekit_api

    try:
        await lkapi.room.update_room_metadata(
            livekit_api.UpdateRoomMetadataRequest(
                room=room_name,
                metadata=json.dumps({"handoff": state}),
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Could not update room state: {exc}", file=sys.stderr)


async def _staff_session(lkapi, url: str, api_key: str, api_secret: str, room_name: str) -> None:
    room = await _connect_staff(url, api_key, api_secret, room_name)
    speaker = StaffSpeaker(room)
    await _mark_room(lkapi, room_name, "staff_joined")
    print(f"Staff joined {room_name}. Type a message and press Enter. /quit leaves.")
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if line == "":
                break
            text = " ".join(line.strip().split())
            if not text:
                continue
            if text == "/quit":
                break
            if len(text) > 400:
                print("Message is too long (400 characters max).", file=sys.stderr)
                continue
            try:
                await speaker.speak(text)
            except Exception as exc:  # noqa: BLE001
                print(f"Could not send that line: {exc}", file=sys.stderr)
    finally:
        await _mark_room(lkapi, room_name, "staff_left")
        await room.disconnect()
        print("Staff left. Waiting for the next transfer.")


def _require_rtc():
    """The API virtualenv ships livekit-api only. Joining a room needs livekit.rtc."""

    try:
        from livekit import rtc
    except ImportError as exc:
        raise SystemExit(
            "This Python cannot join a LiveKit room: livekit.rtc is not installed.\n"
            "The API virtualenv only has livekit-api. Use the voice-agent environment:\n"
            "  cd services/livekit-agent\n"
            "  uv run python human_simulator.py"
        ) from exc
    return rtc


async def main() -> None:
    _require_rtc()
    from livekit import api as livekit_api

    url, api_key, api_secret = _livekit_credentials()
    lkapi = livekit_api.LiveKitAPI(url, api_key, api_secret)
    print("Human simulator waiting. It joins a test room after the voice agent leaves.")
    print("Force-closing the agent counts. Type a line once Staff joined appears.")
    try:
        while True:
            room_name = await _wait_for_handoff(lkapi)
            try:
                await _staff_session(lkapi, url, api_key, api_secret, room_name)
            except ImportError as exc:
                raise SystemExit(
                    "This Python cannot join a LiveKit room: livekit.rtc is not installed.\n"
                    "Leave the API virtualenv and run: uv run python human_simulator.py"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                print(f"Could not join {room_name}: {exc}", file=sys.stderr)
                await asyncio.sleep(1)
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nHuman simulator stopped.")
