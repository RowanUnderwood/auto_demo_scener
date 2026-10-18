"""Demo source store and tool definitions for the iterative generation strategy.

Everything here is pure (no I/O, no LLM, no browser) so it can be exercised directly with
simulated model output. The session loop that drives these lives in agent_runner.py.

Turns are stateless and executed serially (each request is rebuilt from session state and
its actions are applied in order before the next request), so a model can never act on a
stale view of the source. That is why the tools take no revision arguments: edits always
apply to the current revision, runs and finishes always target the current revision.
"""
import json
import re

# ── Source store ───────────────────────────────────────────────────────────────


class EditError(Exception):
    """An edit that can't be applied; `code` is a stable identifier the model sees."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SourceStore:
    """Holds every revision of the demo. Revision 1 is the classic-stream first draft."""

    def __init__(self, html: str):
        self.revisions: dict[int, str] = {1: html}
        self.current = 1

    @property
    def source(self) -> str:
        return self.revisions[self.current]

    def _commit(self, html: str) -> int:
        self.current += 1
        self.revisions[self.current] = html
        return self.current

    def write(self, html: str) -> tuple[int, tuple[int, int]]:
        """Replace the whole document. Returns (new revision, changed line range)."""
        if not html or not html.strip():
            raise EditError("empty_html", "write_demo needs the complete HTML document.")
        rev = self._commit(html)
        return rev, (1, html.count("\n") + 1)

    def edit(self, old_text: str, new_text: str) -> tuple[int, tuple[int, int]]:
        """Replace exactly one occurrence of old_text. Falls back to a whitespace-tolerant
        match (runs of whitespace compare equal) because models routinely get indentation
        wrong, but the match must still be unique. Returns (new revision, changed lines)."""
        if not old_text or not old_text.strip():
            raise EditError("empty_old_text", "old_text must be a non-empty snippet copied from the source.")
        src = self.source
        count = src.count(old_text)
        if count == 1:
            start = src.index(old_text)
            end = start + len(old_text)
        elif count > 1:
            raise EditError("ambiguous_match",
                            f"old_text matches {count} places; include more surrounding lines so it is unique.")
        else:
            tokens = old_text.split()
            pattern = re.compile(r"\s+".join(re.escape(t) for t in tokens))
            matches = list(pattern.finditer(src))
            if len(matches) == 1:
                start, end = matches[0].span()
            elif len(matches) > 1:
                raise EditError("ambiguous_match",
                                f"old_text matches {len(matches)} places; include more surrounding lines so it is unique.")
            else:
                raise EditError("no_match",
                                "old_text was not found in the current source. Copy it exactly from the "
                                "CURRENT SOURCE shown (without the line-number prefixes).")
        new_src = src[:start] + new_text + src[end:]
        rev = self._commit(new_src)
        first = src.count("\n", 0, start) + 1
        return rev, (first, first + new_text.count("\n"))

    def numbered(self) -> str:
        return "\n".join(f"{i:4}| {line}" for i, line in enumerate(self.source.split("\n"), 1))


# ── Tool definitions ───────────────────────────────────────────────────────────

TOOL_NAMES = ("edit_demo", "write_demo", "run_demo", "finish_demo")

TOOLS = [
    {"type": "function", "function": {
        "name": "edit_demo",
        "description": ("Replace one exact snippet of the current demo source. old_text must be "
                        "copied from the current source (without line-number prefixes) and match "
                        "exactly one place. Several edit_demo calls may be made in one response; "
                        "they apply in order."),
        "parameters": {"type": "object", "properties": {
            "old_text": {"type": "string", "description": "Exact snippet to replace (unique)."},
            "new_text": {"type": "string", "description": "Replacement text."},
            "change_summary": {"type": "string", "description": "One line: what this fixes and why."},
        }, "required": ["old_text", "new_text", "change_summary"]},
    }},
    {"type": "function", "function": {
        "name": "write_demo",
        "description": ("Replace the whole demo with a complete new HTML document. Only use this "
                        "when targeted edits can't fix the problem."),
        "parameters": {"type": "object", "properties": {
            "html": {"type": "string", "description": "The complete HTML document."},
            "change_summary": {"type": "string", "description": "One line: why a rewrite was needed."},
        }, "required": ["html", "change_summary"]},
    }},
    {"type": "function", "function": {
        "name": "run_demo",
        "description": ("Run the current revision in the kiosk browser and record diagnostics. The "
                        "app also runs it automatically after your changes, so call this only to "
                        "request a longer run."),
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "number", "description": "Run length in seconds (capped by the app)."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "finish_demo",
        "description": ("Declare the current revision done. Accepted only if its latest run passed; "
                        "if you changed the source in this response it is run first."),
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "One line describing the final demo."},
        }, "required": ["summary"]},
    }},
]

JSON_ACTIONS_INSTRUCTIONS = """
RESPONSE FORMAT: reply with ONE JSON object and nothing else (no markdown fences):
{"actions": [{"action": "<tool name>", "arguments": {...}}, ...]}
Available actions and their arguments:
- edit_demo: {"old_text": str, "new_text": str, "change_summary": str}
- write_demo: {"html": str, "change_summary": str}
- run_demo: {"seconds": number (optional)}
- finish_demo: {"summary": str}
Actions run in order. Escape newlines and quotes inside strings as JSON requires."""


# ── Action parsing ─────────────────────────────────────────────────────────────


class ActionParseError(Exception):
    pass


_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


def actions_from_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Native tool calls → [{"action", "arguments"}]. Raises ActionParseError if any call has
    an unknown name or arguments that aren't a complete JSON object (never partially applied)."""
    actions = []
    for tc in tool_calls:
        name = tc.get("name") or ""
        if name not in TOOL_NAMES:
            raise ActionParseError(f"unknown tool {name!r}")
        raw = tc.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            raise ActionParseError(f"arguments for {name} are not valid JSON ({e.msg})") from e
        if not isinstance(args, dict):
            raise ActionParseError(f"arguments for {name} must be a JSON object")
        actions.append({"action": name, "arguments": args})
    return actions


def actions_from_content(content: str) -> list[dict]:
    """JSON-action fallback: parse {"actions": [...]} (or a single action object) out of the
    response text. Prose is never executed — anything that isn't such an object is an error."""
    text = _THINK_RE.sub("", content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ActionParseError("no JSON object found in the response")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ActionParseError(f"response is not valid JSON ({e.msg} at char {e.pos})") from e
    items = obj.get("actions") if isinstance(obj, dict) and "actions" in obj else [obj]
    if not isinstance(items, list) or not items:
        raise ActionParseError('expected {"actions": [ ... ]} with at least one action')
    actions = []
    for item in items:
        if not isinstance(item, dict):
            raise ActionParseError("each action must be a JSON object")
        name = item.get("action") or item.get("name") or item.get("tool")
        args = item.get("arguments", item.get("args", {}))
        if name not in TOOL_NAMES:
            raise ActionParseError(f"unknown action {name!r}")
        if not isinstance(args, dict):
            raise ActionParseError(f"arguments for {name} must be a JSON object")
        actions.append({"action": name, "arguments": args})
    return actions
