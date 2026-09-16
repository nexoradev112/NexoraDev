`uv` is installed as a Python package, but the `uv` command is not on your PATH. Use this instead:

```powershell
python -m uv sync --extra dev
python -m uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

I already ran the sync. `backend/.venv` is created and the 60 packages are installed.

From `backend` you can also skip `uv` on PATH entirely:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Migrate first if you have not (Postgres must be up):

```powershell
python -m uv run python -m app.cli migrate
```


Start the worker from `services/livekit-agent` (PowerShell). `.env.local` is already there.

First time (Silero VAD models):

```powershell
cd services\livekit-agent
python -m uv sync --frozen
python -m uv run python agent.py download-files
python -m uv run python agent.py dev
```

After that, only:

```powershell
cd services\livekit-agent
python -m uv run python agent.py dev
```

Keep FastAPI on `127.0.0.1:8000` running. Leave this terminal open.

The worker is required for a real voice conversation. **Start voice test** can still show “Live · microphone on” because LiveKit realtime is platform-funded. When the worker joins, it asks FastAPI for **LLM + STT + TTS**. Tenant Deepgram / ElevenLabs / OpenAI keys in Settings are the preferred path.

If STT or TTS is not saved, the session still starts: the UI flashes a notice, and the worker uses the same LiveKit Inference STT/LLM/TTS stack as AICMS v6/v7. Typed chat still uses your tenant LLM key. Add STT/TTS in Settings when you want your own speech providers.