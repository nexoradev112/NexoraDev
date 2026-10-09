# Local development

Start each process in its own terminal, from the repository root.

## Frontend

```powershell
npm run dev
```

## Backend API

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API listens on `http://127.0.0.1:8000`.

## Voice agent

```powershell
cd services/livekit-agent
.\.venv\Scripts\Activate.ps1
python agent.py dev
```
