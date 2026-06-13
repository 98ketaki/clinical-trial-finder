"""Plain-language match explanations via Anthropic Claude.

One batched call covers all matches in a request (lower latency/cost than per-trial).
If Claude is unavailable or returns malformed output, every match falls back to a
templated explanation so POST /match never hard-fails on this step.
"""

import os
import json
import logging
from typing import Any, Dict, List

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("ctgov_explain")

CLAUDE_MODEL = "claude-haiku-4-5"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

SYSTEM_PROMPT = (
    "You explain, in plain language a patient can understand, why a clinical trial "
    "may be relevant to them. Be warm, concrete, and honest about caveats. 2-3 "
    "sentences per trial. Never give medical advice or tell the patient what to do; "
    "frame trials as options to discuss with their oncologist. Do not invent "
    "eligibility details beyond what is provided."
)


def get_anthropic_client():
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("anthropic is required. Install it with `pip install anthropic`.") from e
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured in the environment.")
    return Anthropic(api_key=ANTHROPIC_API_KEY)


def _is_retryable_anthropic_error(exc: BaseException) -> bool:
    try:
        import anthropic
    except ImportError:
        return False
    retryable = (
        anthropic.RateLimitError,
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
        anthropic.InternalServerError,
    )
    return isinstance(exc, retryable)


def _template(profile: Dict[str, Any], match: Dict[str, Any]) -> str:
    """Deterministic fallback explanation."""
    title = match.get("title") or "This trial"
    bits = []
    crit = match.get("criteria") or {}
    if crit.get("cancer_type"):
        bits.append(f"targets {crit['cancer_type']}")
    if crit.get("stages"):
        bits.append(f"enrolls stage {', '.join(crit['stages'])}")
    detail = (" It " + " and ".join(bits) + ".") if bits else ""
    return (
        f"{title} came up as a semantic match for your profile.{detail} "
        "Review the eligibility details with your oncologist to confirm fit."
    )


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception(_is_retryable_anthropic_error),
    reraise=True,
)
def _call_claude(client: Any, prompt: str) -> str:
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")


def explain_matches(profile: Dict[str, Any], matches: List[Dict[str, Any]]) -> Dict[str, str]:
    """Return {nct_id: explanation}. Falls back to templates on any failure."""
    if not matches:
        return {}

    fallback = {m["nct_id"]: _template(profile, m) for m in matches}

    try:
        client = get_anthropic_client()
    except Exception as e:
        logger.warning("Claude unavailable (%s); using templated explanations", e)
        return fallback

    trials_payload = [
        {"nct_id": m["nct_id"], "title": m.get("title"), "criteria": m.get("criteria")}
        for m in matches
    ]
    prompt = (
        "Patient profile:\n"
        f"{json.dumps(profile, ensure_ascii=False, default=str)}\n\n"
        "Candidate trials:\n"
        f"{json.dumps(trials_payload, ensure_ascii=False, default=str)}\n\n"
        "Return ONLY a JSON object mapping each nct_id to a 2-3 sentence plain-language "
        "explanation of why it may be relevant to this patient. No prose outside the JSON."
    )

    try:
        raw = _call_claude(client, prompt)
        # Tolerate stray text/markdown fences around the JSON object.
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object in response")
        parsed = json.loads(raw[start:end + 1])
    except Exception as e:
        logger.warning("Claude explanation parse failed (%s); using templated explanations", e)
        return fallback

    # Merge: use Claude's text where present, template otherwise.
    out = dict(fallback)
    for nct_id, text in parsed.items():
        if nct_id in out and isinstance(text, str) and text.strip():
            out[nct_id] = text.strip()
    return out
