"""
LLM-based recipe extraction (engineering path for ad-heavy pages).

Uses SpaceXAI / xAI (OpenAI-compatible API):
  XAI_API_KEY + https://api.x.ai/v1

The model sees raw page/PDF text and returns ONLY structured recipe JSON.
Ads, sign-in walls, nutrition widgets, and "related recipes" are instructed
out. TRN conversion then runs on clean data only.

Fallback: if no key or API error, callers use heuristic extraction.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv

from .recipe_draft import RecipeDraft

load_dotenv()

# Prefer XAI; allow OPENAI-compatible override for self-host / other clouds later
XAI_BASE = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1").rstrip("/")
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4.5")
EXTRACT_MAX_CHARS = 28000  # keep prompt bounded

SYSTEM = """You are a recipe extraction engine for a cooking app.
Given messy webpage or PDF text (with ads, sign-in prompts, nutrition labels,
related articles, comments, equipment lists, affiliate junk), extract ONE
primary recipe as clean JSON.

Rules:
- Output ONLY valid JSON, no markdown fences, no commentary.
- Ignore ads, visit/subscribe/sign-in, newsletters, comments, related recipes,
  nutrition facts, SEO keyword lists, author bios, and equipment-only lists
  (smoker, grill) unless they are also ingredients.
- If the page has multiple sub-recipes (e.g. salsa + beans + assembly), merge
  into ONE recipe with all ingredients and steps in cooking order, OR pick the
  main dish and put sub-recipes' steps in order after prep components.
- Ingredients: one string per line with amounts (e.g. "2 cups flour").
- Instructions: one clear imperative step per item, in order. No step numbers.
- Title: the dish name only (never "Sign In", "Equipment", "Ingredients").
- If you cannot find a real recipe, return {"title":"","ingredients":[],"instructions":[]}.

JSON schema:
{
  "title": "string",
  "servings": "string or null",
  "ingredients": ["string", ...],
  "instructions": ["string", ...],
  "notes": "string optional short notes only"
}
"""


def llm_available() -> bool:
    return bool(os.environ.get("XAI_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def extract_recipe_llm(
    raw_text: str,
    *,
    source: str = "",
    title_hint: Optional[str] = None,
    timeout: int = 90,
) -> Tuple[Optional[RecipeDraft], Optional[str]]:
    """
    Returns (RecipeDraft, error_message).
    On success error is None. On failure draft is None.
    """
    api_key = os.environ.get("XAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, "No XAI_API_KEY (or OPENAI_API_KEY) set"

    text = (raw_text or "").strip()
    if len(text) < 40:
        return None, "Text too short for LLM extract"

    # Prefer the densest middle of long pages (often the recipe card)
    if len(text) > EXTRACT_MAX_CHARS:
        text = _window_around_recipe(text, EXTRACT_MAX_CHARS)

    user = "Extract the recipe from this text.\n"
    if title_hint:
        user += f"Likely title hint: {title_hint}\n"
    if source:
        user += f"Source: {source}\n"
    user += "\n---\n" + text + "\n---\nReturn JSON only."

    base = XAI_BASE
    model = XAI_MODEL
    # If only OPENAI_API_KEY, use OpenAI endpoint unless XAI_BASE overridden
    if not os.environ.get("XAI_API_KEY") and os.environ.get("OPENAI_API_KEY"):
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user},
                ],
            },
            timeout=timeout,
        )
        if resp.status_code >= 400:
            return None, f"LLM API HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        draft = _parse_llm_json(content, source=source)
        if not draft or not draft.ingredients or not draft.instructions:
            return None, "LLM returned empty/incomplete recipe"
        draft.extractor = "llm"
        if title_hint and (
            not draft.title
            or draft.title.lower() in {"untitled recipe", "sign in", "equipment"}
        ):
            draft.title = title_hint
        return draft, None
    except requests.RequestException as e:
        return None, f"LLM request failed: {e}"
    except Exception as e:
        return None, f"LLM extract error: {e}"


def _parse_llm_json(content: str, source: str = "") -> Optional[RecipeDraft]:
    if not content:
        return None
    content = content.strip()
    # Strip fences if model ignores instructions
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    # Find outermost JSON object
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        # trailing commas
        cleaned = re.sub(r",\s*}", "}", content[start : end + 1])
        cleaned = re.sub(r",\s*]", "]", cleaned)
        try:
            d = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
    if not isinstance(d, dict):
        return None
    d["source"] = source or d.get("source") or ""
    d["extractor"] = "llm"
    return RecipeDraft.from_dict(d)


def _window_around_recipe(text: str, max_chars: int) -> str:
    """Keep a window centered on Ingredients/Instructions if present."""
    lower = text.lower()
    anchors = [
        lower.find("\ningredients"),
        lower.find("\ningredient"),
        lower.find("recipeingredient"),
        lower.find("\ndirections"),
        lower.find("\ninstructions"),
    ]
    anchors = [a for a in anchors if a >= 0]
    if not anchors:
        return text[:max_chars]
    mid = min(anchors)
    start = max(0, mid - max_chars // 4)
    return text[start : start + max_chars]
