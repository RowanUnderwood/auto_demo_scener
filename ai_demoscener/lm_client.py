import json
import logging

import requests

log = logging.getLogger(__name__)


class LMClientError(Exception):
    pass


class LengthFinishError(LMClientError):
    pass


def list_models(base_url: str, timeout: int = 10) -> list[str]:
    try:
        r = requests.get(f"{base_url}/v1/models", timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return [m["id"] for m in data.get("data", [])]
    except Exception as e:
        raise LMClientError(f"Failed to list models: {e}") from e


def chat_stream(
    messages: list[dict],
    base_url: str,
    model: str,
    max_tokens: int,
    temperature: float,
    timeout: int = 180,
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


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
