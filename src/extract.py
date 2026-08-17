"""
Unified recipe extraction pipeline (engineering path).

  messy input → RecipeDraft → TRNTable

Ads, paywalls, related posts, and nutrition widgets never enter TRN logic.
TRN only sees a clean RecipeDraft.

Extract order (first success that passes a basic draft check wins):
  1. Already-structured input (schema.org / JSON-LD shaped text from fetch)
  2. LLM (XAI_API_KEY / SpaceXAI) — required for real blog/magazine pages
  3. Heuristic section parse (clean paste / simple PDFs only)

If the page is messy and LLM is off, we still try heuristic but the quality
gate fails closed and the UI tells you to turn on extract (or paste/PDF).

This design is laptop-first today; the same extract_and_convert() call is what
a cloud worker would run later — only the host of XAI_API_KEY changes.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .recipe_draft import RecipeDraft
from .llm_extract import extract_recipe_llm, llm_available
from .models import TRNTable
from .trn_converter import convert_text_to_trn
from .sanitize import sanitize_recipe_text
from .text_cleanup import cleanup_recipe_text
from .quality import assess_conversion


# Signals that raw text still has page chrome (heuristic alone will struggle)
_MESSY_PATTERNS = re.compile(
    r"(?i)("
    r"sign\s*in|log\s*in|subscribe|newsletter|related\s+recipes|"
    r"you\s+may\s+also|jump\s+to\s+recipe|print\s+recipe|pin\s+it|"
    r"affiliate|sponsored|cookie\s+policy|privacy\s+policy|"
    r"calories|nutrition\s+facts|facebook|pinterest|instagram|"
    r"leave\s+a\s+comment|write\s+a\s+review|rate\s+this"
    r")"
)


def extract_and_convert(
    raw_text: str,
    *,
    source: str = "",
    title_hint: Optional[str] = None,
    prefer_llm: bool = True,
    fetch_method: str = "",
) -> Tuple[TRNTable, RecipeDraft, dict]:
    """
    Returns (table, draft, meta) where meta has extractor + quality + errors.
    """
    meta: dict = {
        "extractor": None,
        "llm_error": None,
        "quality_score": None,
        "quality_ok": None,
        "issues": [],
        "messy": False,
        "fetch_method": fetch_method or "",
        "llm_available": llm_available(),
        "hint": "",
    }

    text = cleanup_recipe_text(raw_text or "")
    text, _ = sanitize_recipe_text(text)

    # Reader/HTML scrapes come from real ad pages even after sanitize trims chrome
    page_fetch = fetch_method in {"reader", "html_scrape", "upload", "ocr"}
    messy = _is_messy(text) or page_fetch
    # JSON-LD is structured food data — ads never appear in the schema block
    if fetch_method == "jsonld":
        messy = False
    meta["messy"] = messy

    draft: Optional[RecipeDraft] = None

    # ── 1) Trust only true schema.org JSON-LD as "already clean" ──────────
    # Do NOT treat sanitized blog text as structured: title often becomes
    # "Related Recipes…" after ads are stripped but chrome headings remain.
    if fetch_method == "jsonld":
        draft = _draft_from_structured_text(text, source=source, title_hint=title_hint)
        if draft and draft.ingredients and draft.instructions:
            meta["extractor"] = "jsonld"
        else:
            draft = None
    elif not page_fetch and _looks_structured(text):
        # Clean paste / simple file with clear Ingredients + Instructions
        draft = _draft_from_structured_text(text, source=source, title_hint=title_hint)
        if draft and draft.ingredients and draft.instructions:
            if _title_looks_like_chrome(draft.title):
                draft = None
            else:
                meta["extractor"] = "structured"
        else:
            draft = None

    # ── 2) LLM — primary path for ad-heavy real-world pages ───────────────
    want_llm = prefer_llm and llm_available() and (
        draft is None or (messy and meta.get("extractor") != "jsonld")
    )
    if want_llm and (draft is None or messy):
        llm_draft, err = extract_recipe_llm(text, source=source, title_hint=title_hint)
        meta["llm_error"] = err
        if llm_draft and llm_draft.ingredients and llm_draft.instructions:
            draft = llm_draft
            meta["extractor"] = "llm"

    # ── 3) Heuristic fallback ─────────────────────────────────────────────
    if draft is None:
        table_h = convert_text_to_trn(text, title=title_hint, source=source)
        preps = [s.detail for s in table_h.steps if s.is_header and s.detail]
        bodies = [s.detail for s in table_h.steps if not s.is_header and s.detail]
        draft = RecipeDraft(
            title=table_h.title,
            ingredients=[i.display_left(1.0) for i in table_h.ingredients],
            instructions=preps + bodies,
            servings=table_h.servings,
            notes=table_h.notes or "",
            source=source,
            extractor="heuristic",
        )
        meta["extractor"] = "heuristic"
        if messy and not llm_available():
            meta["hint"] = (
                "This page looks ad-heavy / messy. Heuristics alone often fail. "
                "Add XAI_API_KEY in the sidebar (or .env) so the LLM extractor "
                "can strip ads before TRN. Or paste only ingredients + steps."
            )
        elif messy and meta.get("llm_error"):
            meta["hint"] = (
                f"LLM extract failed ({meta['llm_error']}); fell back to heuristics."
            )

    # ── Draft → TRN (never feed raw ads into cascade logic) ───────────────
    table = _table_from_draft(draft, source=source)

    if title_hint and (
        not table.title
        or table.title.lower()
        in {"untitled recipe", "sign in", "equipment", "recipe", "empty recipe"}
    ):
        table.title = title_hint
        draft.title = title_hint

    report = assess_conversion(table)
    meta["quality_score"] = report.score
    meta["quality_ok"] = report.ok
    meta["issues"] = report.issues

    # ── Second chance: quality failed on heuristic → try LLM if available ─
    if (
        not report.ok
        and meta["extractor"] in {"heuristic", "structured"}
        and prefer_llm
        and llm_available()
    ):
        draft2, err2 = extract_recipe_llm(text, source=source, title_hint=title_hint)
        meta["llm_error"] = err2
        if draft2 and draft2.ingredients and draft2.instructions:
            draft = draft2
            table = _table_from_draft(draft, source=source)
            if draft.title:
                table.title = draft.title
            if draft.servings:
                table.servings = draft.servings
            meta["extractor"] = "llm"
            report = assess_conversion(table)
            meta["quality_score"] = report.score
            meta["quality_ok"] = report.ok
            meta["issues"] = report.issues
            meta["hint"] = ""

    if not report.ok and messy and not llm_available():
        meta["hint"] = (
            "Conversion failed quality gate on a messy page without LLM extract. "
            "Set XAI_API_KEY (sidebar or .env from console.x.ai) and convert again — "
            "or paste only the recipe card / upload a recipe-only PDF."
        )

    return table, draft, meta


def _table_from_draft(draft: RecipeDraft, source: str = "") -> TRNTable:
    """
    Build TRN from clean draft only.

    Uses the text serializer + convert_text_to_trn so prep headers / cascade
    match the rest of the app. Title/servings/source forced from draft.
    """
    table = convert_text_to_trn(
        _draft_as_text(draft), title=draft.title, source=source or draft.source
    )
    if draft.title:
        table.title = draft.title
    if draft.servings:
        table.servings = draft.servings
    table.source = source or draft.source or table.source
    if draft.notes and not table.notes:
        table.notes = draft.notes
    return table


def _title_looks_like_chrome(title: str) -> bool:
    if not title:
        return True
    return bool(
        re.match(
            r"(?i)^(sign\s*in|log\s*in|equipment|ingredients?|directions?|"
            r"related|you\s+may|more\s+recipes|newsletter|subscribe|"
            r"recipes?|home|comment|print|share)\b",
            title.strip(),
        )
    )


def _is_messy(text: str) -> bool:
    if not text or len(text) < 40:
        return False
    hits = len(_MESSY_PATTERNS.findall(text))
    # Long pages with many chrome hits = blog/magazine dump
    if hits >= 4:
        return True
    if hits >= 2 and len(text) > 6000:
        return True
    # Glued words / no clear Ingredients block often means OCR or HTML dump
    if "Ingredients" not in text and "ingredients" not in text.lower():
        if hits >= 1:
            return True
    return False


def _looks_structured(text: str) -> bool:
    """True if text already looks like our clean Ingredients/Instructions form."""
    low = text.lower()
    has_ing = bool(re.search(r"(?m)^ingredients?\s*$", text, re.I))
    has_dir = bool(
        re.search(r"(?m)^(?:instructions?|directions?|method)\s*$", text, re.I)
    )
    qty = sum(
        1
        for ln in text.splitlines()
        if re.match(r"^[\-\*]?\s*[\d½¼¾⅓⅔]", ln.strip())
        or re.search(r"\b(cup|tbsp|tsp|oz|lb)\b", ln, re.I)
    )
    chrome = len(_MESSY_PATTERNS.findall(text))
    return has_ing and has_dir and qty >= 3 and chrome < 3 and len(text) < 12000


def _draft_from_structured_text(
    text: str, source: str = "", title_hint: Optional[str] = None
) -> Optional[RecipeDraft]:
    """Parse our own clean 'Ingredients / Instructions' layout into a draft."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    title = title_hint or ""
    servings = None
    ingredients: list[str] = []
    instructions: list[str] = []
    section = None

    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if re.match(r"(?i)^servings?\s*:", s):
            servings = re.sub(r"(?i)^servings?\s*:\s*", "", s).strip()
            continue
        if re.match(r"(?i)^ingredients?\s*$", s):
            section = "ing"
            continue
        if re.match(r"(?i)^(?:instructions?|directions?|method)\s*$", s):
            section = "dir"
            continue
        if section is None and i < 8:
            if re.match(r"(?i)^(servings?|yield)\b", s):
                continue
            if _title_looks_like_chrome(s):
                continue
            if not title:
                title = s
            continue
        if section == "ing":
            ingredients.append(re.sub(r"^[\-\*\u2022•]\s*", "", s))
        elif section == "dir":
            instructions.append(re.sub(r"^\d+[\.\)]\s*", "", s))

    if not ingredients or not instructions:
        return None
    final_title = title_hint or title or "Untitled Recipe"
    if _title_looks_like_chrome(final_title) and title_hint:
        final_title = title_hint
    if _title_looks_like_chrome(final_title) and title and not _title_looks_like_chrome(title):
        final_title = title
    return RecipeDraft(
        title=final_title,
        ingredients=ingredients,
        instructions=instructions,
        servings=servings,
        source=source,
        extractor="structured",
    )


def _draft_as_text(draft: RecipeDraft) -> str:
    """Serialize draft to the plain format convert_text_to_trn expects."""
    parts = [draft.title or "Untitled Recipe"]
    if draft.servings:
        parts.append(f"Servings: {draft.servings}")
    parts.append("")
    parts.append("Ingredients")
    for line in draft.ingredients:
        parts.append(f"- {line}" if not line.startswith("-") else line)
    parts.append("")
    parts.append("Instructions")
    for i, step in enumerate(draft.instructions, 1):
        parts.append(f"{i}. {step}")
    if draft.notes:
        parts.append("")
        parts.append(draft.notes)
    return "\n".join(parts)
