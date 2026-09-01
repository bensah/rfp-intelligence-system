"""Shared model-chain + fallback helper for the LLM call sites (judge, synthesis).

WHY a chain: every LLM call is one-shot (max_retries=0) and, on failure, drops
straight to the deterministic regex path. That is fine for a transient blip, but
when the free-tier weekly QUOTA runs out mid-scan, EVERY remaining call fails and
the rest of the scan silently extracts less — the "burns through the allocation
before we've extracted everything" symptom. Ollama Cloud's free tier exposes
several models (gpt-oss:120b / gpt-oss:20b / gemma3 / nemotron-3 …); a fallback
chain lets an exhausted or unavailable primary degrade to ANOTHER free model
instead of to regex. It also enables cost routing: point a feature's primary at a
cheaper model and keep the big one only as a fallback (or vice-versa) — all via
env, no code change.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def model_chain(model_env: str, default: str) -> list[str]:
    """Ordered list of models to try for a feature.

    Resolution (de-duplicated, order preserved):
      primary   = $<model_env>  or  $LLM_JUDGE_MODEL  or  <default>
      fallbacks = $<feature>_FALLBACK_MODELS (csv)  or  $LLM_FALLBACK_MODELS (csv)

    where <feature> is model_env without its trailing ``_MODEL`` (so
    ``LLM_JUDGE_MODEL`` → ``LLM_JUDGE_FALLBACK_MODELS``)."""
    primary = os.environ.get(model_env) or os.environ.get("LLM_JUDGE_MODEL") or default
    fb_env = model_env[:-6] + "_FALLBACK_MODELS" if model_env.endswith("_MODEL") \
        else model_env + "_FALLBACK_MODELS"
    raw = os.environ.get(fb_env) or os.environ.get("LLM_FALLBACK_MODELS") or ""
    chain = [primary] + [m.strip() for m in raw.split(",") if m.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for m in chain:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def chat_with_fallback(client, models, **create_kwargs):
    """Call ``client.chat.completions.create`` against each model in ``models`` until
    one returns non-empty content. Any exception (quota / rate-limit / timeout /
    unknown model) or an empty reply advances to the next model.

    Returns ``(content, model_used, response)``; ``("", "", None)`` if all fail.
    ``create_kwargs`` are forwarded verbatim (do NOT include ``model``)."""
    last_exc: Exception | None = None
    for m in models:
        try:
            resp = client.chat.completions.create(model=m, **create_kwargs)
            content = (resp.choices[0].message.content or "") if resp.choices else ""
            if content:
                if last_exc is not None:
                    log.info("llm fell back to model %s after earlier failure", m)
                return content, m, resp
            last_exc = RuntimeError("empty response")
        except Exception as exc:  # noqa: BLE001 — try the next model on ANY failure
            last_exc = exc
            log.warning("llm model %s failed: %s: %s; trying next fallback",
                        m, type(exc).__name__, exc)
            continue
    if last_exc is not None:
        log.warning("all llm models failed %s: %s", models, last_exc)
    return "", "", None
