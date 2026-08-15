"""
Shared, robust JSON extraction for Claude responses that are SUPPOSED to
be JSON-only. Every Claude call site in this codebase used to strip
markdown fences with a fragile inline pattern:

    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()

That only works if the ENTIRE response starts with the fence -- any
leading prose before a fenced block ("Sure, here's my assessment:\n```json
...") or trailing prose after one, or a fence-free response with stray
text around the JSON, made json.loads() fail even though a well-formed
JSON object was right there in the response. See the Aug 2026
content_policy/quality "Extra data"/"Expecting value" parse-failure
incident, hit during live testing, that traced back to this exact gap
repeated at 21 call sites across 17 files.

extract_json_text() is strictly MORE permissive than the old inline
logic, never less -- a response that's already exactly a bare JSON
object/array (the common, instructed case) passes through unchanged, so
this is a safe drop-in replacement everywhere, not a behavior change for
the case that was already working.
"""
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json_text(text: str) -> str:
    """
    Best-effort extraction of the JSON object/array from a Claude text
    response. Always returns SOME string for json.loads() to attempt --
    if nothing looks extractable, returns the (stripped) input unchanged
    so the caller's own try/except and fallback still applies exactly as
    before.
    """
    text = text.strip()
    if not text:
        return text

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1).strip()

    # No fence -- if a JSON object/array appears ANYWHERE in the text
    # (e.g. surrounded by stray prose despite "JSON only" instructions),
    # extract the outermost {...} or [...] span instead of handing the
    # whole blob straight to json.loads().
    candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not candidates:
        return text
    first = min(candidates)
    close_char = "}" if text[first] == "{" else "]"
    last = text.rfind(close_char)
    if last > first:
        return text[first:last + 1]
    return text
