from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .licensing import (
    LicenseClaims,
    active_license_for_workspace,
    adjust_consumed_quota,
    consume_quota,
    require_feature,
)
from .models import UsageEvent, Workspace
from .provider_vault import RuntimeCredential, enforce_platform_model, resolve_runtime_credential

FIXED_LLM_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
}


@dataclass(frozen=True)
class CompletionResult:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int


class ProviderResponseError(Exception):
    def __init__(self, message: str, *, cost_uncertain: bool) -> None:
        super().__init__(message)
        self.cost_uncertain = cost_uncertain


def _openai_compatible(
    credential: RuntimeCredential,
    model: str,
    messages: list[dict[str, str]],
) -> CompletionResult:
    response = httpx.post(
        FIXED_LLM_ENDPOINTS[credential.provider],
        headers={"Authorization": f"Bearer {credential.secret}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 1200},
        timeout=30,
    )
    if response.status_code >= 400:
        raise ProviderResponseError(
            f"{credential.provider} request failed",
            cost_uncertain=False,
        )
    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderResponseError(
            f"{credential.provider} returned an invalid response",
            cost_uncertain=True,
        ) from exc
    if not isinstance(text, str):
        raise ProviderResponseError(
            f"{credential.provider} returned an invalid response",
            cost_uncertain=True,
        )
    return CompletionResult(
        text=text,
        provider=credential.provider,
        model=model,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


def _anthropic(
    credential: RuntimeCredential,
    model: str,
    messages: list[dict[str, str]],
) -> CompletionResult:
    system = "\n".join(message["content"] for message in messages if message["role"] == "system")
    conversation = [message for message in messages if message["role"] != "system"]
    response = httpx.post(
        FIXED_LLM_ENDPOINTS["anthropic"],
        headers={
            "x-api-key": credential.secret,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={"model": model, "system": system, "messages": conversation, "max_tokens": 1200},
        timeout=30,
    )
    if response.status_code >= 400:
        raise ProviderResponseError("anthropic request failed", cost_uncertain=False)
    try:
        data = response.json()
        text = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
        usage = data.get("usage", {})
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderResponseError("anthropic returned an invalid response", cost_uncertain=True) from exc
    if not text:
        raise ProviderResponseError("anthropic returned an invalid response", cost_uncertain=True)
    return CompletionResult(
        text=text,
        provider="anthropic",
        model=model,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def create_chat_completion(
    db: Session,
    claims: LicenseClaims,
    settings: Settings,
    provider_order: list[str],
    model: str,
    messages: list[dict[str, str]],
    *,
    feature: str,
    allowed_models: Mapping[str, Collection[str]] | None = None,
) -> CompletionResult:
    # Reserve against an upper-bound estimate before using a platform-funded
    # provider. The final counter uses provider-reported tokens when available.
    # UTF-8 byte length is a conservative token upper bound across the supported
    # Arabic, Devanagari, and Latin scripts. It avoids under-reserving paid usage.
    estimated_input = max(1, sum(len(item["content"].encode("utf-8")) for item in messages))
    last_configuration_error: HTTPException | None = None
    attempted = False
    model_was_rejected = False
    for provider in provider_order:
        if provider not in FIXED_LLM_ENDPOINTS:
            continue
        # Every attempt gets a fresh entitlement after locking the tenant.
        # This prevents a fallback provider from starting after a concurrent
        # revoke, expiry, feature downgrade, or provider-mode change.
        db.scalar(
            select(Workspace)
            .where(Workspace.id == claims.license.workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_claims = active_license_for_workspace(db, claims.license.workspace_id, settings, lock=True)
        if current_claims.license.id != claims.license.id:
            raise HTTPException(status_code=402, detail="License changed during provider request")
        require_feature(current_claims, feature)
        try:
            credential = resolve_runtime_credential(db, current_claims, "llm", provider, settings)
        except HTTPException as exc:
            if exc.status_code == 503:
                last_configuration_error = exc
                continue
            raise
        provider_model = model
        if provider == "anthropic" and not model.startswith("claude-"):
            provider_model = credential.config.get("model", "claude-3-5-haiku-latest")
        elif provider == "groq" and not model.startswith(("llama", "mixtral", "gemma")):
            provider_model = credential.config.get("model", "llama-3.3-70b-versatile")
        provider_model = enforce_platform_model("llm", provider, provider_model, credential.source)
        if allowed_models is not None and provider_model not in allowed_models.get(provider, ()):
            model_was_rejected = True
            continue
        reserved = estimated_input + 1200 if credential.source == "platform" else 0
        if reserved:
            # Reserve and commit before the paid network call. This keeps the
            # database lock short and ensures later validation failures cannot
            # roll provider spend out of the usage ledger.
            consume_quota(db, current_claims, "tokens", reserved)
        # Release tenant/license/provider locks before any external request,
        # including BYOK attempts that do not reserve platform quota.
        db.commit()
        attempted = True
        try:
            result = (
                _anthropic(credential, provider_model, messages)
                if provider == "anthropic"
                else _openai_compatible(credential, provider_model, messages)
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            if reserved:
                # A timeout can happen after an upstream accepted/billed the
                # request. Retain the ceiling reservation rather than silently
                # shifting ambiguous provider spend onto the platform.
                db.add(
                    UsageEvent(
                        workspace_id=current_claims.license.workspace_id,
                        license_id=current_claims.license.id,
                        kind="tokens_uncertain",
                        quantity=reserved,
                        provider=provider,
                    )
                )
                db.commit()
            continue
        except ProviderResponseError as exc:
            if reserved:
                if exc.cost_uncertain:
                    db.add(
                        UsageEvent(
                            workspace_id=current_claims.license.workspace_id,
                            license_id=current_claims.license.id,
                            kind="tokens_uncertain",
                            quantity=reserved,
                            provider=provider,
                        )
                    )
                else:
                    adjust_consumed_quota(db, current_claims, "tokens", -reserved)
                db.commit()
            continue
        total = result.input_tokens + result.output_tokens
        if total <= 0:
            total = estimated_input + max(1, len(result.text) // 3)
        db.scalar(
            select(Workspace)
            .where(Workspace.id == claims.license.workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        final_claims = active_license_for_workspace(db, claims.license.workspace_id, settings, lock=True)
        if final_claims.license.id != claims.license.id:
            raise HTTPException(status_code=402, detail="License changed during provider request")
        require_feature(final_claims, feature)
        if reserved:
            adjust_consumed_quota(db, final_claims, "tokens", total - reserved)
        db.add(
            UsageEvent(
                workspace_id=final_claims.license.workspace_id,
                license_id=final_claims.license.id,
                kind="tokens",
                quantity=total,
                provider=result.provider,
            )
        )
        db.commit()
        return result
    if last_configuration_error:
        raise last_configuration_error
    if model_was_rejected and not attempted:
        raise HTTPException(status_code=422, detail="No allowlisted LLM model was available")
    if attempted:
        raise HTTPException(status_code=502, detail="Configured LLM providers did not complete the request")
    raise HTTPException(status_code=502, detail="No configured LLM provider was available")


def extract_json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise HTTPException(status_code=502, detail="Model did not return a JSON object")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Model returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail="Model returned invalid JSON")
    return value
