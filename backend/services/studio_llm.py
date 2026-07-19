from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List

import httpx

from backend.llm_analyzer import FALLBACK_MODEL, MODEL_DEFAULT, _ollama_generate
from backend.services import model_service

SUPPORTED_PROVIDERS = ("ollama", "gemini", "lmstudio")
PROVIDER_LMSTUDIO = "lmstudio"  # named constant used by bootstrap and routing guards

_GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
_GEMINI_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
_GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-pro").strip()
_GEMINI_MODELS = [
    item.strip()
    for item in os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-pro").split(",")
    if item.strip()
]

_LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234").rstrip("/")
_LM_STUDIO_DEFAULT_MODEL = os.getenv("LM_STUDIO_MODEL", "").strip()

_MIN_TOKENS = 256          # floor for max_tokens / maxOutputTokens passed to any provider
_MAX_ERROR_TEXT = 240      # truncation limit for raw provider error bodies logged to callers


def normalize_provider(value: str | None) -> str:
    provider = (value or "").strip().lower()
    return provider if provider in SUPPORTED_PROVIDERS else "ollama"


def provider_catalog() -> List[Dict[str, Any]]:
    # LM Studio is considered configured when LM_STUDIO_URL is explicitly set;
    # Ollama is always available as the default local provider.
    return [
        {"key": "ollama", "label": "Ollama", "configured": True},
        {"key": "gemini", "label": "Gemini", "configured": bool(os.getenv("GEMINI_API_KEY", "").strip())},
        {"key": "lmstudio", "label": "LM Studio", "configured": bool(os.getenv("LM_STUDIO_URL", "").strip())},
    ]


def default_provider() -> str:
    return normalize_provider(os.getenv("STUDIO_LLM_PROVIDER", "ollama"))


def default_model(provider: str | None) -> str:
    provider_key = normalize_provider(provider)
    if provider_key == "gemini":
        return _GEMINI_DEFAULT_MODEL
    if provider_key == "lmstudio":
        return _LM_STUDIO_DEFAULT_MODEL
    return MODEL_DEFAULT


async def _fetch_lmstudio_models(timeout: float = 10.0) -> List[str]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{_LM_STUDIO_URL}/v1/models")
        if response.status_code != 200:
            return []
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return sorted(
            {item["id"] for item in data if isinstance(item, dict) and item.get("id")},
            key=str.lower,
        )
    except Exception:
        return []


async def models_payload(provider: str | None = None, timeout: float = 10.0) -> Dict[str, Any]:
    provider_key = normalize_provider(provider or default_provider())
    payload: Dict[str, Any] = {
        "provider": provider_key,
        "providers": provider_catalog(),
        "models": [],
        "default": default_model(provider_key),
        "fallback": _GEMINI_FALLBACK_MODEL if provider_key == "gemini" else FALLBACK_MODEL,
    }

    if provider_key == "gemini":
        payload["models"] = list(_GEMINI_MODELS)
        if not os.getenv("GEMINI_API_KEY", "").strip():
            payload["error"] = "Gemini API key is not configured."
        return payload

    if provider_key == "lmstudio":
        models = await _fetch_lmstudio_models(timeout=timeout)
        payload["models"] = models
        payload["fallback"] = None
        if not models:
            payload["error"] = "LM Studio is not reachable or has no loaded models."
        return payload

    result = await model_service.fetch_tags(timeout=timeout)
    payload["models"] = result["models"]
    if not result["ok"]:
        payload["error"] = result["error"]
    return payload


def _normalize_model_name(model: str | None, provider: str) -> str:
    candidate = (model or "").strip()
    if candidate:
        try:
            if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", candidate):
                return candidate
        except Exception:
            return candidate
    return default_model(provider)


async def _generate_with_ollama(prompt: str, model: str, timeout: float, num_predict: int) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _ollama_generate(
            prompt=prompt,
            model=model,
            timeout=timeout,
            json_only=False,
            options_overrides={"num_predict": num_predict},
        ),
    )


def _extract_gemini_text(payload: Dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return ""
    chunks: list[str] = []
    for candidate in candidates:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip():
                chunks.append(text)
    return "\n".join(chunks).strip()


async def _generate_with_gemini(prompt: str, model: str, timeout: float, num_predict: int) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Gemini API key is not configured.")

    url = f"{_GEMINI_API_BASE}/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": max(_MIN_TOKENS, num_predict),
        },
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, params={"key": api_key}, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini error {response.status_code}: {response.text[:_MAX_ERROR_TEXT]}")
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"Gemini returned invalid JSON: {exc}") from exc
    text = _extract_gemini_text(payload)
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text


async def _generate_with_lmstudio(prompt: str, model: str, timeout: float, num_predict: int) -> str:
    url = f"{_LM_STUDIO_URL}/v1/chat/completions"
    body: Dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max(_MIN_TOKENS, num_predict),
        "temperature": 0.2,
    }
    if model:
        body["model"] = model
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"LM Studio error {response.status_code}: {response.text[:_MAX_ERROR_TEXT]}")
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"LM Studio returned invalid JSON: {exc}") from exc
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LM Studio returned an unexpected response format: {exc}") from exc
    if not text:
        raise RuntimeError("LM Studio returned an empty response.")
    return str(text)


async def generate_text(
    *,
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    timeout: float = 45.0,
    num_predict: int = 512,
) -> Dict[str, str]:
    provider_key = normalize_provider(provider or default_provider())
    model_name = _normalize_model_name(model, provider_key)

    if provider_key == "gemini":
        text = await _generate_with_gemini(prompt=prompt, model=model_name, timeout=timeout, num_predict=num_predict)
    elif provider_key == "lmstudio":
        text = await _generate_with_lmstudio(prompt=prompt, model=model_name, timeout=timeout, num_predict=num_predict)
    else:
        available = await model_service.fetch_tags(timeout=min(timeout, 5.0))
        discovered_models = available.get("models") if isinstance(available, dict) else []
        fallback_candidates: list[str] = [model_name, MODEL_DEFAULT]
        if isinstance(discovered_models, list) and discovered_models:
            fallback_candidates.append(str(discovered_models[0]))
        if FALLBACK_MODEL:
            fallback_candidates.append(FALLBACK_MODEL)

        last_error: Exception | None = None
        seen: set[str] = set()
        text = ""
        resolved_model = model_name
        for candidate in fallback_candidates:
            candidate_name = (candidate or "").strip()
            if not candidate_name or candidate_name in seen:
                continue
            seen.add(candidate_name)
            try:
                text = await _generate_with_ollama(prompt=prompt, model=candidate_name, timeout=timeout, num_predict=num_predict)
                resolved_model = candidate_name
                break
            except Exception as exc:
                last_error = exc
                continue
        if not text:
            raise RuntimeError(str(last_error) if last_error else "No working Ollama model is available.")
        model_name = resolved_model

    return {"provider": provider_key, "model": model_name, "text": (text or "").strip()}
