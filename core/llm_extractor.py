"""LLM-assisted extraction layer (Anthropic Claude).

What this is for
----------------
The regex/PDF/Playwright pipeline in `core/scraper.py` handles the
common shape of donor RFP pages — a labelled deadline, plain text
eligibility, a clean PDF guide. It cannot read:

  * date-window banners that live inside images
    (e.g. Fondation Pierre Fabre's "FROM OCT 9TH TO NOV 7TH 2025")
  * deadlines inside table layouts in PDFs that pypdf flattens
  * cross-site companion pages (donor links out to a partner site
    where the real timeline lives)
  * non-English page bodies that an English regex misses

For those cases this module sends the page text (and, optionally, the
companion PDF text) to Claude and parses a strict JSON response with
the four fields we care about: `submission_deadline`, `eligibility_text`,
`brief_description`, `confidence`.

How keys are managed
--------------------
Any Anthropic Console workspace can issue API keys at
console.anthropic.com. Paste the key into `.env` as
`ANTHROPIC_API_KEY=sk-ant-...`. The `.env` file is already gitignored —
verified at `.gitignore:1`. The key is read at runtime by
`os.environ.get("ANTHROPIC_API_KEY")` and never written anywhere else.
Deploying orgs (CHAI BDT being the reference deployment) should use a
workspace-scoped key owned by their org, not a personal one.

If `ANTHROPIC_API_KEY` is not set, every public function in this module
returns `None` and the calling code falls back to the regex pipeline.
That makes the LLM layer fully optional — the app works without it,
the user can flip it on by adding one env-var entry.

Cost ceiling
------------
Defaults to `claude-3-5-haiku-latest` (cheapest tier). At typical donor-
page sizes (1–4 KB of body text after stripping nav chrome) one
extraction is well under 2K input tokens + 300 output tokens. Roughly
$0.0008 per call — negligible at Enterprise-tier scale. Model can be
overridden via `LLM_EXTRACT_MODEL` if needed.

Hard guardrails
---------------
  * No retries on auth/rate-limit errors — fail fast, fall back to regex.
  * 12 second hard timeout per call.
  * JSON-only response; if Claude returns anything else, we log + return None.
  * Only called when the regex pipeline left key fields blank, so the
    LLM is purely additive (never overrides a confident regex result).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# Defaults are read at call time (not import time) so a .env change
# during a Streamlit hot-reload picks up the new key without restart.
_DEFAULT_MODEL = "claude-3-5-haiku-latest"
_TIMEOUT_SECONDS = 12
_MAX_INPUT_CHARS = 6000   # ~1.5K tokens — keeps cost bounded


def is_enabled() -> bool:
    """True iff ANTHROPIC_API_KEY is set + anthropic SDK is installed.
    Cheap probe — safe to call on every candidate."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401, WPS433
    except ImportError:
        return False
    return True


def _build_prompt(title: str, url: str, page_text: str) -> str:
    """Build the user-facing prompt. Kept in one place so it's easy to
    tune without touching SDK boilerplate."""
    # Trim hard so we don't blow the input-token budget on long pages.
    snippet = (page_text or "").strip()[:_MAX_INPUT_CHARS]
    return (
        "You are extracting structured facts from a donor RFP / call-for-"
        "proposals page.\n\n"
        f"TITLE: {title}\n"
        f"URL: {url}\n"
        f"PAGE TEXT (truncated to {_MAX_INPUT_CHARS} chars):\n"
        "<<<\n"
        f"{snippet}\n"
        ">>>\n\n"
        "Return ONE JSON object — no prose, no markdown fences — with "
        "these keys:\n"
        '  "submission_deadline": ISO date "YYYY-MM-DD" of the FINAL\n'
        "    application closing date. If the page describes a window\n"
        '    like "FROM Oct 9 TO Nov 7 2025", return the END date\n'
        "    (Nov 7 = 2025-11-07). If no concrete deadline appears,\n"
        "    return null. Do NOT guess from publication / cohort years.\n"
        '  "eligibility_text": one-sentence summary of who can apply\n'
        '    (e.g. "Open to non-profit organizations in sub-Saharan\n'
        '    Africa working on digital health"). null if unclear.\n'
        '  "brief_description": one or two sentences explaining what\n'
        "    the donor is funding. Plain prose, no marketing fluff.\n"
        '    null if the page is too vague.\n'
        '  "confidence": "high" if you found explicit deadline /\n'
        '    eligibility text; "medium" if inferred from context;\n'
        '    "low" if you mostly guessed.\n\n'
        "If the page is clearly NOT an RFP (it's a generic landing\n"
        "page, a search results list, a grantee profile, etc.), return\n"
        "all four fields as null and set confidence to \"low\"."
    )


_DEADLINE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def extract(
    *,
    title: str,
    url: str,
    page_text: str,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Call Claude to extract deadline + eligibility + description.

    Returns dict with keys (submission_deadline, eligibility_text,
    brief_description, confidence) or None if the call failed / LLM
    is disabled. Returned date is an ISO string ("YYYY-MM-DD"), not a
    `date` object — callers convert as needed.
    """
    if not is_enabled():
        return None
    if not page_text or len(page_text.strip()) < 50:
        return None  # not enough signal to bother

    try:
        import anthropic  # noqa: WPS433 (lazy import; optional dep)
    except ImportError:
        return None

    chosen_model = model or os.environ.get("LLM_EXTRACT_MODEL", _DEFAULT_MODEL)
    prompt = _build_prompt(title, url, page_text)

    try:
        client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            timeout=_TIMEOUT_SECONDS,
        )
        resp = client.messages.create(
            model=chosen_model,
            max_tokens=400,
            system=(
                "You are a careful information extractor. Return ONLY "
                "valid JSON matching the requested schema, nothing else. "
                "When unsure, return null rather than guessing."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("LLM extraction failed for %s: %s: %s",
                    url, type(exc).__name__, exc)
        return None

    raw = ""
    try:
        # The Anthropic SDK returns a list of content blocks; the model
        # is configured to return a single text block.
        for block in resp.content or []:
            if getattr(block, "type", "") == "text":
                raw += getattr(block, "text", "")
    except Exception:
        raw = ""

    parsed = _parse_json_block(raw)
    if not parsed:
        log.info("LLM returned non-JSON for %s: %r", url, raw[:200])
        return None

    # Normalise + validate
    dl = parsed.get("submission_deadline")
    if dl and not (isinstance(dl, str) and _DEADLINE_ISO_RE.match(dl)):
        dl = None
    return {
        "submission_deadline": dl,
        "eligibility_text": _str_or_none(parsed.get("eligibility_text")),
        "brief_description": _str_or_none(parsed.get("brief_description")),
        "confidence": _str_or_none(parsed.get("confidence")) or "low",
        "_llm_model": chosen_model,
    }


def _parse_json_block(raw: str) -> dict[str, Any] | None:
    """Tolerant JSON parser — strips markdown fences, picks first object."""
    if not raw:
        return None
    s = raw.strip()
    # Strip ```json ... ``` fences if the model added them despite instructions.
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # Find the first '{' and the matching '}' (greedy — Claude rarely nests).
    i = s.find("{")
    j = s.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        return json.loads(s[i : j + 1])
    except json.JSONDecodeError:
        return None


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
