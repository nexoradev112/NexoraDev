"use client";

import type { Room } from "livekit-client";
import { useEffect, useRef, useState } from "react";

type ChatLine = { role: "system" | "agent" | "user" | "staff"; text: string };

function participantLabel(identity: string, name: string, event: "joined" | "left") {
  if (identity === "staff" || name === "Staff") {
    return event === "joined" ? "Staff joined." : "Staff left.";
  }
  return event === "joined" ? "Voice agent joined the test room." : "Voice agent left the test room.";
}

export default function TestSession({ agentId, workspaceId, agentName, brand, onClose }: { agentId: number; workspaceId: number; agentName: string; brand: string; onClose: () => void }) {
  const roomRef = useRef<Room | null>(null);
  const audioRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Ready");
  const [micOn, setMicOn] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [voiceNotice, setVoiceNotice] = useState("");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatLine[]>([{ role: "system", text: "Start voice when you are ready, or test by typing below." }]);

  useEffect(() => () => { void roomRef.current?.disconnect(); roomRef.current = null; }, []);

  async function startVoice() {
    if (connecting || roomRef.current) return;
    setConnecting(true);
    setStatus("Connecting…");
    const { Room, RoomEvent, Track } = await import("livekit-client");
    const room = new Room({ adaptiveStream: true, dynacast: true });
    room.on(RoomEvent.TrackSubscribed, track => {
      if (track.kind !== Track.Kind.Audio || !audioRef.current) return;
      const element = track.attach();
      element.autoplay = true;
      audioRef.current.appendChild(element);
    });
    room.on(RoomEvent.TrackUnsubscribed, track => track.detach());
    room.on(RoomEvent.Disconnected, () => { setStatus("Disconnected"); setMicOn(false); roomRef.current = null; });
    room.on(RoomEvent.ParticipantConnected, (participant) => setMessages(current => [...current, { role: "system", text: participantLabel(participant.identity, participant.name || "", "joined") }]));
    room.on(RoomEvent.ParticipantDisconnected, (participant) => setMessages(current => [...current, { role: "system", text: participantLabel(participant.identity, participant.name || "", "left") }]));
    room.on(RoomEvent.DataReceived, (payload, participant) => {
      if (!participant || (participant.identity !== "staff" && participant.name !== "Staff")) return;
      const text = new TextDecoder().decode(payload).trim();
      if (text) setMessages(current => [...current, { role: "staff", text }]);
    });
    try {
      const response = await fetch("/api/livekit/token", { method: "POST", headers: { "content-type": "application/json", "x-workspace-id": String(workspaceId) }, body: JSON.stringify({ agentId, sessionId: crypto.randomUUID() }) });
      const data = await response.json() as { server_url?: string; participant_token?: string; error?: string; voice_notice?: string; voiceNotice?: string };
      if (!response.ok || !data.server_url || !data.participant_token) throw new Error(data.error || "Realtime voice is unavailable");
      const notice = data.voice_notice || data.voiceNotice || "";
      if (notice) {
        setVoiceNotice(notice);
      }
      await room.connect(data.server_url, data.participant_token);
      await room.localParticipant.setMicrophoneEnabled(true);
      roomRef.current = room;
      setMicOn(true);
      setStatus("Live · microphone on");
    } catch (error) {
      await room.disconnect();
      setStatus("Voice unavailable");
      setMessages(current => [...current, { role: "system", text: error instanceof Error ? error.message : "Could not connect to realtime voice" }]);
    } finally {
      setConnecting(false);
    }
  }

  async function toggleMic() {
    const room = roomRef.current;
    if (!room) return;
    const next = !micOn;
    await room.localParticipant.setMicrophoneEnabled(next);
    setMicOn(next);
    setStatus(next ? "Live · microphone on" : "Live · microphone muted");
  }

  async function sendText() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    setMessages(current => [...current, { role: "user", text }]);
    try {
      const response = await fetch("/api/chat", { method: "POST", headers: { "content-type": "application/json", "x-workspace-id": String(workspaceId) }, body: JSON.stringify({ agentId, messages: [{ role: "user", content: text }] }) });
      const data = await response.json() as { text?: string; error?: string };
      setMessages(current => [...current, data.text ? { role: "agent", text: data.text } : { role: "system", text: data.error || "Chat request failed" }]);
    } catch {
      setMessages(current => [...current, { role: "system", text: "Chat request failed" }]);
    }
  }

  return <div className="test-drawer" role="dialog" aria-label={`Test ${agentName}`}>
    <div><b>Test {agentName}</b><button type="button" onClick={onClose} aria-label="Close test">×</button></div>
    <p className="test-status"><span className={micOn ? "voice-live" : ""}>●</span> {status}</p>
    {voiceNotice ? <p className="settings-notice" role="status">{voiceNotice}</p> : null}
    <div className="test-feed">{messages.map((message, index) => <p key={`${message.role}-${index}`} className={message.role === "user" ? "user-msg" : message.role === "agent" ? "agent-msg" : "system-msg"}>{message.role === "agent" ? <small>{brand} agent</small> : message.role === "staff" ? <small>Staff</small> : null}{message.text}</p>)}</div>
    <div ref={audioRef} hidden/>
    <div className="voice-controls"><button type="button" onClick={() => void (roomRef.current ? toggleMic() : startVoice())} disabled={connecting}>{roomRef.current ? (micOn ? "Mute microphone" : "Unmute microphone") : "Start voice test"}</button></div>
    <div className="test-compose"><span aria-hidden="true">⌨</span><input value={input} placeholder="Type a message…" onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === "Enter") void sendText(); }}/><button type="button" onClick={() => void sendText()} aria-label="Send message">↑</button></div>
  </div>;
}
