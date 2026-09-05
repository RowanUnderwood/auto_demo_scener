import json
import logging

import requests

log = logging.getLogger(__name__)


class LMClientError(Exception):
    pass


_EMBEDDING_PATTERNS = ("embed", "nomic", "bge-", "e5-", "rerank")


class LengthFinishError(LMClientError):
    pass


def list_models(base_url: str, timeout: int = 10,
                exclude_embedding: bool = False) -> list[str]:
    try:
        r = requests.get(f"{base_url}/v1/models", timeout=timeout)
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
        if exclude_embedding:
            ids = [m for m in ids
                   if not any(p in m.lower() for p in _EMBEDDING_PATTERNS)]
        return ids
    except Exception as e:
        raise LMClientError(f"Failed to list models: {e}") from e


def chat_stream(
    messages: list[dict],
    base_url: str,
    model: str,
    max_tokens: int,
    temperature: float,
    timeout: int = 180,
    extra_params: dict | None = None,
):
    """Yield content-delta strings from a streaming chat completion.

    Raises LengthFinishError if the model hit the token limit.
    Raises LMClientError for network/HTTP failures.
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if extra_params:
        payload.update(extra_params)
    log.debug("LLM request: model=%s max_tokens=%d input_msgs=%d", model, max_tokens, len(messages))

    try:
        with requests.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            finish_reason = None
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices", []):
                    content = choice.get("delta", {}).get("content")
                    if content:
                        yield content
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr

            log.debug("LLM stream done: finish_reason=%s", finish_reason)
            if finish_reason == "length":
                raise LengthFinishError("Model stopped at token limit")

    except LengthFinishError:
        raise
    except requests.exceptions.Timeout as e:
        raise LMClientError(f"Request timed out after {timeout}s") from e
    except requests.exceptions.ConnectionError as e:
        raise LMClientError(f"Connection error: {e}") from e
    except requests.exceptions.HTTPError as e:
        raise LMClientError(f"HTTP error {e.response.status_code}: {e}") from e


def chat(
    messages: list[dict],
    base_url: str,
    model: str,
    max_tokens: int | None = 128,
    temperature: float = 0.7,
    timeout: int = 30,
    extra_params: dict | None = None,
) -> str:
    """Non-streaming chat completion. Returns the full response string."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if extra_params:
        payload.update(extra_params)
    try:
        r = requests.post(f"{base_url}/v1/chat/completions", json=payload, timeout=timeout)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return content if content is not None else ""
    except Exception as e:
        raise LMClientError(f"Chat failed: {e}") from e


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
