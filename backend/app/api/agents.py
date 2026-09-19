from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import ROLE_LEVEL, WorkspaceAccess, require_workspace
from ..licensing import require_feature
from ..models import Agent, AgentVersion, AuditLog, Integration, StoredFile, Workspace
from ..provider_vault import SUPPORTED_PROVIDERS

router = APIRouter(tags=["agents"])

LOCALES = {"ar", "en-US", "en-GB", "hi-IN", "hi-en", "auto"}
CHANNELS = {"chat", "voice", "voice + chat"}
STATUSES = {"draft", "published", "paused", "archived"}
NODE_TYPES = {
    "Trigger",
    "Agent",
    "Knowledge",
    "Router",
    "Guardrail",  # human-approval workflow gate; platform safety rails are independent
    "Tool",
    "Handoff",
    "Message",
    "Audio",
    "Webhook",
    "QA",
    "End",
}
IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,99}$")
_NODE_TYPE_ALIASES = {
    "trigger": "Trigger",
    "start": "Trigger",
    "startevent": "Trigger",
    "input": "Trigger",
    "agent": "Agent",
    "assistant": "Agent",
    "llm": "Agent",
    "knowledge": "Knowledge",
    "rag": "Knowledge",
    "router": "Router",
    "condition": "Router",
    "decision": "Router",
    "guardrail": "Guardrail",
    "approval": "Guardrail",
    "tool": "Tool",
    "action": "Tool",
    "handoff": "Handoff",
    "transfer": "Handoff",
    "human": "Handoff",
    "message": "Message",
    "audio": "Audio",
    "webhook": "Webhook",
    "qa": "QA",
    "end": "End",
    "output": "End",
    "finish": "End",
}
_GENERATED_SKIP_TYPES = {"Audio", "Webhook", "QA"}
SECRET_MATERIAL = re.compile(
    r"(?:(?<![\w-])sk-[A-Za-z0-9_-]{16,}|\b(?:api[_ -]?key|access[_ -]?token|password)"
    r"\s*[:=]\s*[^\s,;]{6,})",
    re.IGNORECASE,
)


def reject_embedded_secret(value: str) -> str:
    if SECRET_MATERIAL.search(value):
        raise ValueError("Agent text must not contain credentials; save provider keys in the encrypted vault")
    return value


def serialize_agent(agent: Agent) -> dict[str, object]:
    return {
        "id": agent.id,
        "workspaceId": agent.workspace_id,
        "name": agent.name,
        "objective": agent.objective,
        "globalPrompt": agent.global_prompt,
        "greeting": agent.greeting,
        "channel": agent.channel,
        "model": agent.model,
        "voice": agent.voice,
        "status": agent.status,
        "recordingEnabled": agent.recording_enabled,
        "maxCallSeconds": agent.max_call_seconds,
        "humanHandoffNumber": agent.human_handoff_number,
        "locale": agent.locale,
        "providerPolicy": agent.provider_policy,
        "workflow": agent.workflow,
        "createdAt": agent.created_at.isoformat(),
        "updatedAt": agent.updated_at.isoformat(),
    }


def validate_workflow(value: Any) -> dict[str, list[dict[str, Any]]]:
    if isinstance(value, list):
        raw_nodes, raw_edges = value, []
    elif isinstance(value, dict):
        raw_nodes, raw_edges = value.get("nodes"), value.get("edges")
    else:
        raise ValueError("Workflow must contain nodes and edges")
    if not isinstance(raw_nodes, list) or len(raw_nodes) > 100:
        raise ValueError("Workflow must contain at most 100 nodes")
    if not isinstance(raw_edges, list) or len(raw_edges) > 250:
        raise ValueError("Workflow must contain at most 250 edges")
    ids: set[str] = set()
    nodes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid workflow node {index + 1}")
        node_id = raw.get("id")
        node_type = raw.get("type")
        label = raw.get("label")
        if (
            not isinstance(node_id, str)
            or not IDENTIFIER.fullmatch(node_id)
            or node_id in ids
            or node_type not in NODE_TYPES
            or not isinstance(label, str)
            or not 1 <= len(label) <= 120
        ):
            raise ValueError(f"Invalid workflow node {index + 1}")
        reject_embedded_secret(node_id)
        ids.add(node_id)
        node: dict[str, Any] = {
            "id": node_id,
            "type": node_type,
            "label": reject_embedded_secret(label.strip()),
        }
        prompt = raw.get("prompt")
        if prompt is not None:
            if not isinstance(prompt, str) or len(prompt) > 8_000:
                raise ValueError("Workflow prompt is too long")
            node["prompt"] = reject_embedded_secret(prompt)
        position = raw.get("position", {"x": 120 + (index % 3) * 280, "y": 80 + (index // 3) * 170})
        if not isinstance(position, dict) or not all(
            isinstance(position.get(k), (int, float)) for k in ("x", "y")
        ):
            raise ValueError("Invalid workflow node position")
        if max(abs(float(position["x"])), abs(float(position["y"]))) > 100_000:
            raise ValueError("Invalid workflow node position")
        node["position"] = {"x": round(float(position["x"]), 2), "y": round(float(position["y"]), 2)}
        config = raw.get("config")
        if config is not None:
            if not isinstance(config, dict) or len(config) > 20:
                raise ValueError("Invalid workflow node configuration")
            clean_config: dict[str, Any] = {}
            allowed_config = {
                "integrationId",
                "path",
                "method",
                "rubric",
                "passThreshold",
                "audioRecordingId",
                "transitionAudioId",
                "approvalRole",
            }
            if set(config) - allowed_config:
                raise ValueError("Workflow node configuration contains unsupported fields")
            for key, item in config.items():
                if isinstance(item, str) and len(item) <= 4_000:
                    clean_config[key] = reject_embedded_secret(item)
                elif isinstance(item, int) and 0 <= item <= 1_000_000_000:
                    clean_config[key] = item
                else:
                    raise ValueError("Workflow node configuration is invalid")
            if node_type == "Guardrail":
                # A studio Guardrail is an approval gate, never a switch for the
                # mandatory Python safety engine.
                clean_config.setdefault("approvalRole", "operator")
            node["config"] = clean_config
        nodes.append(node)
    if sum(node["type"] == "Webhook" for node in nodes) > 8:
        raise ValueError("Workflow may contain at most 8 post-call webhook nodes")
    if sum(node["type"] == "QA" for node in nodes) > 4:
        raise ValueError("Workflow may contain at most 4 post-call QA nodes")
    edge_ids: set[str] = set()
    edges: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid workflow edge {index + 1}")
        edge_id, source, target = raw.get("id"), raw.get("source"), raw.get("target")
        if (
            not isinstance(edge_id, str)
            or not IDENTIFIER.fullmatch(edge_id)
            or edge_id in edge_ids
            or source not in ids
            or target not in ids
            or source == target
        ):
            raise ValueError(f"Invalid workflow edge {index + 1}")
        edge_ids.add(edge_id)
        edge: dict[str, Any] = {"id": edge_id, "source": source, "target": target}
        for key, limit in (("label", 120), ("condition", 500)):
            item = raw.get(key)
            if item is not None:
                if not isinstance(item, str) or len(item) > limit:
                    raise ValueError(f"Invalid workflow edge {key}")
                edge[key] = reject_embedded_secret(item)
        edges.append(edge)
    normalized = {"nodes": nodes, "edges": edges}
    if len(json.dumps(normalized, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise ValueError("Workflow exceeds the 256 KiB runtime limit")
    return normalized


def _text_field(value: Any, limit: int) -> str:
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, dict):
        for key in ("label", "text", "name", "title", "prompt"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()[:limit]
    return ""


def _workflow_id(value: Any, fallback: str, used: set[str]) -> str:
    raw = re.sub(r"[^a-zA-Z0-9._:/-]+", "-", _text_field(value, 100)).strip("-._:/")
    if not raw or not IDENTIFIER.fullmatch(raw):
        raw = fallback
    base = raw[:100]
    candidate = base
    suffix = 2
    while candidate in used:
        extra = f"-{suffix}"
        candidate = f"{base[: 100 - len(extra)]}{extra}"
        suffix += 1
    used.add(candidate)
    return candidate


def _generated_node_type(raw: dict[str, Any]) -> str | None:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    for candidate in (raw.get("type"), raw.get("kind"), data.get("kind"), data.get("type")):
        if candidate in NODE_TYPES:
            return str(candidate)
        if not isinstance(candidate, str):
            continue
        mapped = _NODE_TYPE_ALIASES.get(re.sub(r"[^a-z0-9]", "", candidate.casefold()))
        if mapped:
            return mapped
    return None


def _generated_position(raw: dict[str, Any], index: int) -> dict[str, float]:
    value = raw.get("position")
    if not isinstance(value, dict):
        data = raw.get("data")
        value = data.get("position") if isinstance(data, dict) else None
    coords: dict[str, float] = {}
    if isinstance(value, dict):
        for axis in ("x", "y"):
            item = value.get(axis)
            try:
                number = float(item)
            except (TypeError, ValueError):
                continue
            if abs(number) <= 100_000:
                coords[axis] = round(number, 2)
    if "x" not in coords or "y" not in coords:
        return {"x": 120 + (index % 3) * 280, "y": 80 + (index // 3) * 170}
    return coords


def coerce_generated_workflow(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Turn model-authored graphs into the studio node/edge schema."""

    if isinstance(value, list):
        raw_nodes, raw_edges = value, []
    elif isinstance(value, dict):
        raw_nodes, raw_edges = value.get("nodes"), value.get("edges")
    else:
        raw_nodes, raw_edges = [], []
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    if not isinstance(raw_edges, list):
        raw_edges = []

    used_ids: set[str] = set()
    id_map: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_nodes[:100]):
        if not isinstance(raw, dict):
            continue
        node_type = _generated_node_type(raw)
        if node_type is None or node_type in _GENERATED_SKIP_TYPES:
            continue
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        original_id = str(raw["id"]).strip() if raw.get("id") is not None else f"node-{index + 1}"
        node_id = _workflow_id(original_id, f"node-{index + 1}", used_ids)
        id_map[original_id] = node_id
        id_map[node_id] = node_id
        label = (
            _text_field(raw.get("label"), 120)
            or _text_field(data.get("label"), 120)
            or _text_field(raw.get("name"), 120)
            or node_type
        )
        prompt = _text_field(raw.get("prompt"), 8_000) or _text_field(data.get("prompt"), 8_000)
        node: dict[str, Any] = {
            "id": node_id,
            "type": node_type,
            "label": label,
            "position": _generated_position(raw, index),
        }
        if prompt:
            node["prompt"] = prompt
        if node_type == "Guardrail":
            node["config"] = {"approvalRole": "operator"}
        nodes.append(node)

    if not nodes:
        nodes = [
            {
                "id": "trigger",
                "type": "Trigger",
                "label": "Start conversation",
                "prompt": "Start when a caller or chat session connects.",
                "position": {"x": 80, "y": 80},
            },
            {
                "id": "agent",
                "type": "Agent",
                "label": "Handle the request",
                "prompt": "Help using only verified information.",
                "position": {"x": 380, "y": 80},
            },
        ]
        used_ids = {node["id"] for node in nodes}

    present = {node["type"] for node in nodes}
    if "Handoff" not in present:
        nodes.append(
            {
                "id": _workflow_id("handoff", "handoff", used_ids),
                "type": "Handoff",
                "label": "Transfer to a person",
                "prompt": "Offer a human handoff when requested or confidence is low.",
                "position": {"x": 980, "y": 180},
            }
        )
    if "End" not in present:
        nodes.append(
            {
                "id": _workflow_id("end", "end", used_ids),
                "type": "End",
                "label": "Summarize and finish",
                "prompt": "Recap the confirmed outcome and close politely.",
                "position": {"x": 1280, "y": 80},
            }
        )

    valid_ids = {node["id"] for node in nodes}
    used_edge_ids: set[str] = set()
    edges: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_edges[:250]):
        if not isinstance(raw, dict):
            continue
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        source = id_map.get(str(raw.get("source") or ""), "")
        target = id_map.get(str(raw.get("target") or ""), "")
        if source not in valid_ids or target not in valid_ids or source == target:
            continue
        edge_id = _workflow_id(
            raw.get("id") or f"e-{source}-{target}-{index + 1}",
            f"e-{index + 1}",
            used_edge_ids,
        )
        edge: dict[str, Any] = {"id": edge_id, "source": source, "target": target}
        label = _text_field(raw.get("label"), 120) or _text_field(data.get("label"), 120)
        condition = _text_field(raw.get("condition"), 500) or _text_field(data.get("condition"), 500)
        if label:
            edge["label"] = label
        if condition:
            edge["condition"] = condition
        edges.append(edge)

    if not edges and len(nodes) > 1:
        for source, target in zip(nodes, nodes[1:], strict=False):
            edges.append(
                {
                    "id": _workflow_id(
                        f"e-{source['id']}-{target['id']}",
                        "e-next",
                        used_edge_ids,
                    ),
                    "source": source["id"],
                    "target": target["id"],
                    "label": "next",
                }
            )
    return validate_workflow({"nodes": nodes, "edges": edges})


class AgentBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int | None = Field(default=None, ge=1, exclude=True)
    name: str = Field(min_length=1, max_length=80)
    objective: str = Field(default="", max_length=2_000)
    global_prompt: str = Field(default="", max_length=8_000, alias="globalPrompt")
    greeting: str = Field(default="Hello, how can I help today?", min_length=1, max_length=500)
    channel: str = "voice"
    model: str = Field(default="gpt-4.1-mini", min_length=1, max_length=100)
    voice: str = Field(default="elevenlabs-rachel", min_length=1, max_length=100)
    locale: str = "en-US"
    provider_policy: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "llm": ["openai"],
            "stt": ["deepgram"],
            "tts": ["elevenlabs"],
            "realtime": ["livekit"],
        },
        alias="providerPolicy",
    )
    workflow: Any = Field(default_factory=lambda: {"nodes": [], "edges": []})
    status: str = "draft"
    recording_enabled: bool = Field(default=False, alias="recordingEnabled")
    max_call_seconds: int = Field(default=1800, ge=30, le=14_400, alias="maxCallSeconds")
    human_handoff_number: str = Field(default="", max_length=20, alias="humanHandoffNumber")

    @field_validator("name", "objective", "global_prompt", "greeting")
    @classmethod
    def no_embedded_credentials(cls, value: str) -> str:
        return reject_embedded_secret(value)

    @field_validator("channel")
    @classmethod
    def channel_allowed(cls, value: str) -> str:
        if value not in CHANNELS:
            raise ValueError("Unsupported channel")
        return value

    @field_validator("locale")
    @classmethod
    def locale_allowed(cls, value: str) -> str:
        if value not in LOCALES:
            raise ValueError("Unsupported locale")
        return value

    @field_validator("status")
    @classmethod
    def status_allowed(cls, value: str) -> str:
        if value not in STATUSES:
            raise ValueError("Unsupported status")
        return value

    @field_validator("model", "voice")
    @classmethod
    def identifier_allowed(cls, value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise ValueError("Invalid provider model identifier")
        return reject_embedded_secret(value)

    @field_validator("human_handoff_number")
    @classmethod
    def handoff_e164(cls, value: str) -> str:
        if value and not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("Handoff number must use E.164 format")
        return value

    @field_validator("provider_policy")
    @classmethod
    def validate_policy(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        defaults = {
            "llm": ["openai"],
            "stt": ["deepgram"],
            "tts": ["elevenlabs"],
            "realtime": ["livekit"],
        }
        if not value:
            return defaults
        if set(value) - set(defaults):
            raise ValueError("Provider policy contains an unsupported capability")
        result = defaults | value
        for kind, order in result.items():
            if not isinstance(order, list) or not 1 <= len(order) <= 5 or len(set(order)) != len(order):
                raise ValueError(f"Invalid {kind} provider order")
            if any(provider not in SUPPORTED_PROVIDERS[kind] for provider in order):
                raise ValueError(f"Unsupported {kind} provider")
        return result

    @field_validator("workflow")
    @classmethod
    def normalized_workflow(cls, value: Any) -> dict[str, list[dict[str, Any]]]:
        return validate_workflow(value)


def validate_referenced_files(
    db: Session,
    access: WorkspaceAccess,
    workflow: dict[str, Any],
    *,
    recording_enabled: bool,
    agent_status: str,
) -> None:
    ids = {
        int(node.get("config", {}).get("audioRecordingId"))
        for node in workflow["nodes"]
        if node.get("type") == "Audio" and isinstance(node.get("config", {}).get("audioRecordingId"), int)
    }
    assert access.license is not None
    if recording_enabled or ids:
        require_feature(access.license, "recordings")
    if not ids:
        return
    records = db.scalars(
        select(StoredFile).where(
            StoredFile.workspace_id == access.workspace.id,
            StoredFile.id.in_(ids),
            StoredFile.category == "recordings",
            StoredFile.status == "ready",
        )
    ).all()
    found = {record.id for record in records}
    if found != ids:
        raise HTTPException(status_code=422, detail="Audio node references an unavailable tenant recording")
    if agent_status == "published" and any(
        record.content_type not in {"audio/wav", "audio/x-wav"}
        or record.details.get("safetyStatus") != "approved"
        for record in records
    ):
        raise HTTPException(
            status_code=422,
            detail="Published Audio nodes require a platform-reviewed WAV recording",
        )


def validate_post_call_permissions_and_integrations(
    db: Session,
    access: WorkspaceAccess,
    workflow: dict[str, Any],
) -> None:
    post_call_nodes = [node for node in workflow["nodes"] if node.get("type") in {"Webhook", "QA"}]
    if not post_call_nodes:
        return
    assert access.license is not None
    require_feature(access.license, "post_call")
    if ROLE_LEVEL.get(access.membership.role, -1) < ROLE_LEVEL["operator"]:
        raise HTTPException(status_code=403, detail="Operator role is required for post-call actions")
    end_nodes = [node["id"] for node in workflow["nodes"] if node.get("type") == "End"]
    if len(end_nodes) != 1:
        raise HTTPException(
            status_code=422,
            detail="A workflow with post-call actions must have exactly one global End node",
        )
    nodes_by_id = {node["id"]: node for node in workflow["nodes"]}
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in workflow["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge)
    current = end_nodes[0]
    seen = {current}
    ordered_post_call: list[str] = []
    while outgoing.get(current):
        edges = outgoing[current]
        if len(edges) != 1:
            raise HTTPException(
                status_code=422,
                detail="The global post-call chain cannot branch",
            )
        edge = edges[0]
        if str(edge.get("condition") or "").strip():
            raise HTTPException(
                status_code=422,
                detail="Post-call routing conditions are not supported in the v1 linear chain",
            )
        target = edge["target"]
        if target in seen:
            raise HTTPException(status_code=422, detail="The post-call chain cannot contain a cycle")
        node = nodes_by_id[target]
        if node.get("type") not in {"Webhook", "QA"}:
            raise HTTPException(
                status_code=422,
                detail="Only QA and Webhook nodes may follow the global End node",
            )
        seen.add(target)
        ordered_post_call.append(target)
        current = target
    if set(ordered_post_call) != {node["id"] for node in post_call_nodes}:
        raise HTTPException(
            status_code=422,
            detail="Every post-call node must belong to one linear chain downstream from End",
        )
    webhook_nodes = [node for node in post_call_nodes if node.get("type") == "Webhook"]
    references = [node.get("config", {}).get("integrationId") for node in webhook_nodes]
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in references):
        raise HTTPException(status_code=422, detail="Every Webhook node must reference one integration")
    ids = set(references)
    if not ids:
        return
    found = set(
        db.scalars(
            select(Integration.id).where(
                Integration.workspace_id == access.workspace.id,
                Integration.id.in_(ids),
                Integration.status == "active",
            )
        ).all()
    )
    if found != ids:
        raise HTTPException(
            status_code=422, detail="Webhook node references an unavailable tenant integration"
        )


def save_version(db: Session, agent: Agent, actor_id: int) -> None:
    latest = (
        db.scalar(
            select(func.max(AgentVersion.version)).where(
                AgentVersion.workspace_id == agent.workspace_id,
                AgentVersion.agent_id == agent.id,
            )
        )
        or 0
    )
    db.add(
        AgentVersion(
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            version=latest + 1,
            definition=serialize_agent(agent),
            created_by_user_id=actor_id,
        )
    )


@router.get("/api/agents")
def list_agents(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "agents"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    agents = db.scalars(
        select(Agent)
        .where(Agent.workspace_id == access.workspace.id)
        .order_by(Agent.updated_at.desc())
        .limit(100)
    ).all()
    return {
        "workspace": {
            "id": access.workspace.id,
            "name": access.workspace.name,
            "role": access.membership.role,
        },
        "agents": [serialize_agent(agent) for agent in agents],
    }


@router.get("/api/agents/definition")
def export_agent_definition(
    id: Annotated[int, Query(ge=1)],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "agents"))],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    agent = db.scalar(select(Agent).where(Agent.id == id, Agent.workspace_id == access.workspace.id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", agent.name).strip("-")[:60] or f"agent-{agent.id}"
    definition = serialize_agent(agent)
    definition.pop("workspaceId", None)
    definition.pop("createdAt", None)
    definition.pop("updatedAt", None)
    return JSONResponse(
        definition,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.json"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/api/agents/definition", status_code=201)
async def import_agent_definition(
    request: Request,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "agents"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    try:
        raw = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Definition must be valid JSON") from exc
    if isinstance(raw, dict) and isinstance(raw.get("agent"), dict):
        raw = raw["agent"]
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Definition must contain an agent object")
    imported = dict(raw)
    imported.pop("id", None)
    imported.pop("workspaceId", None)
    imported.pop("createdAt", None)
    imported.pop("updatedAt", None)
    imported["status"] = "draft"
    body = AgentBody.model_validate(imported)
    return create_agent(body, access, db)


@router.get("/api/agents/versions")
def list_agent_versions(
    id: Annotated[int, Query(ge=1)],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "agents"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    if not db.scalar(select(Agent.id).where(Agent.id == id, Agent.workspace_id == access.workspace.id)):
        raise HTTPException(status_code=404, detail="Agent not found")
    rows = db.scalars(
        select(AgentVersion)
        .where(AgentVersion.agent_id == id, AgentVersion.workspace_id == access.workspace.id)
        .order_by(AgentVersion.version.desc())
        .limit(100)
    ).all()
    return {
        "versions": [
            {
                "id": row.id,
                "version": row.version,
                "createdAt": row.created_at.isoformat(),
                "definition": row.definition,
            }
            for row in rows
        ]
    }


@router.get("/api/agents/{agent_id}")
def get_agent(
    agent_id: int,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "agents"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == access.workspace.id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"agent": serialize_agent(agent)}


@router.post("/api/agents", status_code=201)
def create_agent(
    body: AgentBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "agents"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    db.scalar(select(Workspace).where(Workspace.id == access.workspace.id).with_for_update())
    assert access.license is not None
    if body.status == "archived":
        raise HTTPException(status_code=422, detail="Create the agent as draft, published, or paused")
    agent_limit = access.license.quotas.get("agents")
    if agent_limit is not None:
        count = (
            db.scalar(
                select(func.count(Agent.id)).where(
                    Agent.workspace_id == access.workspace.id,
                    Agent.status != "archived",
                )
            )
            or 0
        )
        if count >= agent_limit:
            raise HTTPException(status_code=402, detail="License agent quota exceeded")
    validate_referenced_files(
        db,
        access,
        body.workflow,
        recording_enabled=body.recording_enabled,
        agent_status=body.status,
    )
    validate_post_call_permissions_and_integrations(db, access, body.workflow)
    agent = Agent(
        workspace_id=access.workspace.id,
        created_by_user_id=access.user.id,
        name=body.name.strip(),
        objective=body.objective,
        global_prompt=body.global_prompt,
        greeting=body.greeting,
        channel=body.channel,
        model=body.model,
        voice=body.voice,
        locale=body.locale,
        provider_policy=body.provider_policy,
        workflow=body.workflow,
        status=body.status,
        recording_enabled=body.recording_enabled,
        max_call_seconds=body.max_call_seconds,
        human_handoff_number=body.human_handoff_number,
    )
    db.add(agent)
    db.flush()
    save_version(db, agent, access.user.id)
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="agent.created",
            resource_type="agent",
            resource_id=str(agent.id),
        )
    )
    db.commit()
    return {"agent": serialize_agent(agent)}


@router.patch("/api/agents")
def update_agent(
    body: AgentBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "agents"))],
    db: Annotated[Session, Depends(get_db)],
    id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    agent_id = id or body.id
    if not agent_id:
        raise HTTPException(status_code=422, detail="Agent id is required")
    agent = db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == access.workspace.id).with_for_update()
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.status == "archived" and body.status != "archived":
        db.scalar(select(Workspace).where(Workspace.id == access.workspace.id).with_for_update())
        assert access.license is not None
        agent_limit = access.license.quotas.get("agents")
        if agent_limit is not None:
            active_count = (
                db.scalar(
                    select(func.count(Agent.id)).where(
                        Agent.workspace_id == access.workspace.id,
                        Agent.status != "archived",
                    )
                )
                or 0
            )
            if active_count >= agent_limit:
                raise HTTPException(status_code=402, detail="License agent quota exceeded")
    validate_referenced_files(
        db,
        access,
        body.workflow,
        recording_enabled=body.recording_enabled,
        agent_status=body.status,
    )
    validate_post_call_permissions_and_integrations(db, access, body.workflow)
    for key in (
        "name",
        "objective",
        "greeting",
        "channel",
        "model",
        "voice",
        "locale",
        "workflow",
        "status",
    ):
        setattr(agent, key, getattr(body, key))
    agent.global_prompt = body.global_prompt
    agent.provider_policy = body.provider_policy
    agent.recording_enabled = body.recording_enabled
    agent.max_call_seconds = body.max_call_seconds
    agent.human_handoff_number = body.human_handoff_number
    db.flush()
    save_version(db, agent, access.user.id)
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="agent.updated",
            resource_type="agent",
            resource_id=str(agent.id),
        )
    )
    db.commit()
    return {"agent": serialize_agent(agent)}


@router.delete("/api/agents")
def archive_agent(
    id: Annotated[int, Query(ge=1)],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "agents"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    agent = db.scalar(
        select(Agent).where(Agent.id == id, Agent.workspace_id == access.workspace.id).with_for_update()
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.status = "archived"
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="agent.archived",
            resource_type="agent",
            resource_id=str(agent.id),
        )
    )
    db.commit()
    return {"archived": True, "id": agent.id}
