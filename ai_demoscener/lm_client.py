import json
import logging

import requests

log = logging.getLogger(__name__)


class LMClientError(Exception):
    pass


_EMBEDDING_PATTERNS = ("embed", "nomic", "bge-", "e5-", "rerank")


class LengthFinishError(LMClientError):
    pass


class LMHTTPError(LMClientError):
    """The server answered with an HTTP error status (e.g. 400 when it rejects `tools`)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _root(base_url: str) -> str:
    """Normalise a base URL so `/v1/...` can be appended. Strata documents its base URL
    with a trailing `/v1` (`http://host:8080/v1`), so accept that form as well as the bare one."""
    url = base_url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def _headers(api_key: str) -> dict:
    """Bearer auth for servers started with an API key (e.g. Strata `--api-key`)."""
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_models(base_url: str, timeout: int = 10,
                exclude_embedding: bool = False, api_key: str = "") -> list[str]:
    try:
        r = requests.get(f"{_root(base_url)}/v1/models", headers=_headers(api_key), timeout=timeout)
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
    api_key: str = "",
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
            f"{_root(base_url)}/v1/chat/completions",
            json=payload,
            headers=_headers(api_key),
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


def chat_collect(
    messages: list[dict],
    base_url: str,
    model: str,
    max_tokens: int,
    temperature: float,
    timeout: int = 180,
    extra_params: dict | None = None,
    api_key: str = "",
    tools: list[dict] | None = None,
    should_abort=None,
) -> dict:
    """Streaming chat completion collected into a structured response:

        {"content": str, "tool_calls": [{"id", "name", "arguments"}], "finish_reason": str|None,
         "usage": dict|None, "aborted": bool}

    Tool-call fragments are assembled by their `index` (argument strings are concatenated,
    never parsed here). Unlike chat_stream(), a `length` finish is returned rather than
    raised, so the caller can treat the response as incomplete. `should_abort()` is polled
    per chunk; when it returns True the stream is closed and `aborted` is set.

    Raises LMClientError for network/HTTP failures (LMHTTPError for a 4xx/5xx response).
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if extra_params:
        payload.update(extra_params)

    content: list[str] = []
    calls: dict[int, dict] = {}
    result = {"content": "", "tool_calls": [], "finish_reason": None, "usage": None, "aborted": False}
    try:
        with requests.post(
            f"{_root(base_url)}/v1/chat/completions",
            json=payload,
            headers=_headers(api_key),
            stream=True,
            timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                # Read the body while the stream is still open — servers explain rejections
                # there (e.g. "started without the vision encoder"), and it's gone afterwards.
                raise LMHTTPError(resp.status_code,
                                  f"HTTP error {resp.status_code}: {resp.text[:300] or resp.reason}")
            for raw_line in resp.iter_lines():
                if should_abort is not None and should_abort():
                    result["aborted"] = True
                    break
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
                if chunk.get("usage"):
                    result["usage"] = chunk["usage"]
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    for pos, tc in enumerate(delta.get("tool_calls") or []):
                        idx = tc.get("index", pos)
                        slot = calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            args = fn["arguments"]
                            # Some servers send the whole arguments object rather than a string
                            slot["arguments"] += args if isinstance(args, str) else json.dumps(args)
                    if choice.get("finish_reason"):
                        result["finish_reason"] = choice["finish_reason"]
    except requests.exceptions.Timeout as e:
        raise LMClientError(f"Request timed out after {timeout}s") from e
    except requests.exceptions.ConnectionError as e:
        raise LMClientError(f"Connection error: {e}") from e

    result["content"] = "".join(content)
    result["tool_calls"] = [calls[i] for i in sorted(calls)]
    log.debug("LLM collect done: finish_reason=%s tool_calls=%d content_chars=%d",
              result["finish_reason"], len(result["tool_calls"]), len(result["content"]))
    return result


def chat(
    messages: list[dict],
    base_url: str,
    model: str,
    max_tokens: int | None = 128,
    temperature: float = 0.7,
    timeout: int = 30,
    extra_params: dict | None = None,
    api_key: str = "",
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
        r = requests.post(f"{_root(base_url)}/v1/chat/completions", json=payload,
                          headers=_headers(api_key), timeout=timeout)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return content if content is not None else ""
    except Exception as e:
        raise LMClientError(f"Chat failed: {e}") from e


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
