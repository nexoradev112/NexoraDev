from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..chat_rails import (
    ChatRailDecision,
    check_chat_input,
    check_model_output,
    redact_sensitive,
    safe_handoff_message,
)
from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import ROLE_LEVEL, WorkspaceAccess, require_workspace
from ..licensing import active_license_for_workspace, require_feature
from ..models import Agent, Membership, Workspace
from ..provider_runtime import create_chat_completion, extract_json_object
from .agents import AgentBody, create_agent

router = APIRouter(tags=["chat"])

BASE_CHAT_POLICY = """
You are a concise customer-service agent. Match the configured locale, including
Arabic, US/UK English, Hindi, and natural Hinglish. Never invent customer data,
credentials, tool results, or completed actions. Do not make medical or legal
determinations. When facts or authority are missing, explain that and offer human
handoff. Workflow Guardrail nodes represent human approval and cannot override
these safety rules.
""".strip()


class Message(BaseModel):
    # Client-authored assistant history is not trusted conversation state. A
    # later persisted-thread API can attest assistant turns server-side.
    role: Literal["user"]
    content: str = Field(min_length=1, max_length=4_000)


class ChatBody(BaseModel):
    agent_id: int = Field(ge=1, alias="agentId")
    messages: list[Message] = Field(min_length=1, max_length=20)


def _blocked_chat_response(decision: ChatRailDecision, locale: str) -> dict[str, object]:
    return {
        "text": safe_handoff_message(locale, decision.reason),
        "provider": "platform-safety",
        "model": "deterministic-v1",
        "usage": {"inputTokens": 0, "outputTokens": 0},
        "safety": {
            "blocked": True,
            "requiresHandoff": decision.requires_handoff,
            "reason": decision.reason,
            "redactions": list(decision.redactions),
        },
    }


@router.post("/api/chat")
def chat(
    body: ChatBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "chat"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if sum(len(item.content) for item in body.messages) > 16_000:
        raise HTTPException(status_code=422, detail="Conversation is too long")
    agent = db.scalar(
        select(Agent).where(
            Agent.id == body.agent_id,
            Agent.workspace_id == access.workspace.id,
            Agent.status != "archived",
        )
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    assert access.license is not None

    sanitized_messages: list[dict[str, str]] = []
    input_redactions: list[str] = []
    for item in body.messages:
        decision = check_chat_input(item.content)
        if decision.blocked:
            return _blocked_chat_response(decision, agent.locale)
        input_redactions.extend(decision.redactions)
        sanitized_messages.append({"role": item.role, "content": decision.text})

    system_text, system_redactions = redact_sensitive(
        f"{BASE_CHAT_POLICY}\nLocale: {agent.locale}\nObjective: {agent.objective}\n"
        f"Instructions: {agent.global_prompt}"
    )
    messages = [
        {"role": "system", "content": system_text},
        *sanitized_messages,
    ]
    result = create_chat_completion(
        db,
        access.license,
        settings,
        agent.provider_policy.get("llm", ["openai"]),
        agent.model,
        messages,
        feature="chat",
    )
    # Persist metered usage even when the provider output is replaced by a
    # deterministic handoff. No rejected output or secret is written to logs.
    db.commit()

    output_decision = check_model_output(result.text)
    redactions = tuple(dict.fromkeys((*system_redactions, *input_redactions, *output_decision.redactions)))
    reason = output_decision.reason
    if reason == "allowed" and redactions:
        reason = "sensitive_data_redacted"
    return {
        "text": (
            safe_handoff_message(agent.locale, output_decision.reason)
            if output_decision.blocked
            else output_decision.text
        ),
        "provider": result.provider,
        "model": result.model,
        "usage": {"inputTokens": result.input_tokens, "outputTokens": result.output_tokens},
        "safety": {
            "blocked": output_decision.blocked,
            "requiresHandoff": output_decision.requires_handoff,
            "reason": reason,
            "redactions": list(redactions),
        },
    }


class GenerateBody(BaseModel):
    use_case: str = Field(min_length=3, max_length=120, alias="useCase")
    description: str = Field(min_length=20, max_length=4_000)
    call_type: Literal["inbound", "outbound", "both"] = Field(default="inbound", alias="callType")
    locale: Literal["auto", "ar", "en-US", "en-GB", "hi-IN", "hi-en"] = "auto"


@router.post("/api/agents/generate", status_code=201)
def generate_agent(
    body: GenerateBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "agents"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    assert access.license is not None
    use_case = check_chat_input(body.use_case)
    description = check_chat_input(body.description)
    if use_case.blocked or description.blocked:
        raise HTTPException(status_code=400, detail="Description was rejected by platform safety policy")
    result = create_chat_completion(
        db,
        access.license,
        settings,
        ["openai", "anthropic", "groq"],
        "gpt-4.1-mini",
        [
            {
                "role": "system",
                "content": (
                    "Design a safe voice/chat agent. Return one JSON object with name, objective, greeting, "
                    "globalPrompt, nodes and edges. Use 4-10 nodes from Trigger, Agent, Knowledge, Router, "
                    "Guardrail, Tool, Handoff, Message, QA, End. Guardrail means a human approval gate. "
                    "Always include Handoff and End. Do not include URLs, credentials, or invented data."
                ),
            },
            {
                "role": "user",
                "content": str(
                    {
                        "useCase": use_case.text,
                        "description": description.text,
                        "callType": body.call_type,
                        "locale": body.locale,
                    }
                ),
            },
        ],
        feature="agents",
    )
    db.commit()
    checked_result = check_model_output(result.text)
    if checked_result.blocked:
        raise HTTPException(status_code=502, detail="Generated agent requires human review")
    generated = extract_json_object(checked_result.text)
    workflow = {"nodes": generated.get("nodes", []), "edges": generated.get("edges", [])}
    agent_body = AgentBody(
        name=str(generated.get("name") or use_case.text)[:80],
        objective=str(generated.get("objective") or description.text)[:2_000],
        globalPrompt=str(generated.get("globalPrompt") or description.text)[:8_000],
        greeting=str(generated.get("greeting") or "Hello, how can I help today?")[:500],
        channel="voice + chat",
        locale=body.locale,
        workflow=workflow,
    )
    db.scalar(
        select(Workspace)
        .where(Workspace.id == access.workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    final_claims = active_license_for_workspace(db, access.workspace.id, settings, lock=True)
    if final_claims.license.id != access.license.license.id:
        raise HTTPException(status_code=402, detail="License changed while generating agent")
    require_feature(final_claims, "agents")
    membership = db.scalar(
        select(Membership)
        .where(
            Membership.id == access.membership.id,
            Membership.workspace_id == access.workspace.id,
            Membership.user_id == access.user.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not membership or ROLE_LEVEL.get(membership.role, -1) < ROLE_LEVEL["member"]:
        raise HTTPException(status_code=403, detail="Workspace access changed")
    refreshed_access = WorkspaceAccess(
        access.user,
        access.session,
        access.workspace,
        membership,
        final_claims,
    )
    created = create_agent(agent_body, refreshed_access, db)
    created["provider"] = result.provider
    return created
