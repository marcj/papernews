from __future__ import annotations

import os

_BACKEND = os.environ.get("LLM_BACKEND", "anthropic").lower()


def chat(system: str, user: str, max_tokens: int) -> str:
    if _BACKEND == "ollama":
        return _ollama(system, user, max_tokens)
    return _anthropic(system, user, max_tokens)


def _anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def _ollama(system: str, user: str, max_tokens: int) -> str:
    import httpx

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "mistral")
    resp = httpx.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "stream": False,
            "options": {"num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=float(os.environ.get("OLLAMA_TIMEOUT", "1800")),
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]
