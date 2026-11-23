"""
Clean intermediate recipe representation.

All messy inputs (URL HTML, PDF, paste) are reduced to RecipeDraft *before*
TRN conversion. The TRN engine only ever sees structured food data — never
ads, nutrition widgets, or "Sign In" chrome.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional
import json
import re
import uuid

from .models import Ingredient, Step, Quantity, TRNTable
from .quantity_parser import parse_ingredient_line
from .sanitize import sanitize_title, scrub_inline, is_junk_ingredient_line, is_junk_step
from .trn_converter import HEADER_ACTIONS, _short_action, _is_junk_step, PROTEIN_WORDS


@dataclass
class RecipeDraft:
    """Normalized recipe — the contract between extractors and TRN."""

    title: str
    ingredients: List[str] = field(default_factory=list)  # free-text lines
    instructions: List[str] = field(default_factory=list)  # free-text steps
    servings: Optional[str] = None
    notes: str = ""
    source: str = ""
    extractor: str = "heuristic"  # heuristic | llm | jsonld

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "RecipeDraft":
        ings = d.get("ingredients") or []
        steps = d.get("instructions") or d.get("steps") or []
        if isinstance(ings, str):
            ings = [ln.strip() for ln in ings.split("\n") if ln.strip()]
        if isinstance(steps, str):
            steps = [ln.strip() for ln in steps.split("\n") if ln.strip()]
        # Normalize list items that are dicts
        clean_ings = []
        for x in ings:
            if isinstance(x, dict):
                clean_ings.append(
                    str(x.get("text") or x.get("item") or x.get("name") or "").strip()
                )
            else:
                clean_ings.append(str(x).strip())
        clean_steps = []
        for x in steps:
            if isinstance(x, dict):
                clean_steps.append(
                    str(x.get("text") or x.get("step") or x.get("instruction") or "").strip()
                )
            else:
                clean_steps.append(str(x).strip())
        return cls(
            title=str(d.get("title") or "Untitled Recipe").strip(),
            ingredients=[x for x in clean_ings if x],
            instructions=[x for x in clean_steps if x],
            servings=(str(d["servings"]).strip() if d.get("servings") else None),
            notes=str(d.get("notes") or "").strip(),
            source=str(d.get("source") or "").strip(),
            extractor=str(d.get("extractor") or "unknown"),
        )


def draft_to_trn(draft: RecipeDraft) -> TRNTable:
    """
    Deterministic: clean draft → TRNTable.
    No ad parsing here — only food lines.
    """
    title = sanitize_title(draft.title)
    ingredients: list[Ingredient] = []
    for line in draft.ingredients:
        line = scrub_inline(line)
        line = re.sub(r"^[\-\*\u2022•\d]+[\.\)]\s*", "", line).strip()
        if not line or is_junk_ingredient_line(line):
            continue
        if re.match(r"(?i)^(ingredients?|for the|optional)\b", line) and ":" in line:
            continue
        ing = parse_ingredient_line(line)
        if ing.name and not is_junk_ingredient_line(ing.name):
            if PROTEIN_WORDS.search(ing.name):
                ing.is_protein = True
            ingredients.append(ing)

    steps: list[Step] = []
    for raw in draft.instructions:
        text = scrub_inline(raw)
        text = re.sub(r"^\d+[\.\)]\s*", "", text).strip()
        if not text or is_junk_step(text) or _is_junk_step(Step(action="", detail=text)):
            continue
        is_header = bool(
            re.match(
                r"(?i)^(preheat|heat\s+(?:your\s+|the\s+)?(?:home\s+)?oven|"
                r"grease|butter and flour|line (?:the )?(?:pan|sheet))\b",
                text,
            )
        )
        # Long steps that also cook are not pure prep
        if is_header and len(text) > 100:
            is_header = False
        action = _short_action(text)
        duration = ""
        dm = re.search(
            r"(\d+\s*(?:to|–|-)?\s*\d*\s*(?:minutes?|mins?|hours?|hrs?))",
            text,
            re.I,
        )
        if dm:
            duration = dm.group(1)
        # Match ingredients by name for cascade
        from .trn_converter import _match_ingredients

        ids = _match_ingredients(text, ingredients)
        steps.append(
            Step(
                action=action,
                detail=text,
                ingredient_ids=ids,
                is_header=is_header,
                duration=duration,
            )
        )

    # Wire simple consumes chain for cascade
    for i, step in enumerate(steps):
        if step.is_header:
            continue
        priors = [j for j in range(i) if not steps[j].is_header]
        if priors:
            step.consumes_steps = [priors[-1]]

    servings = draft.servings
    if servings:
        servings = scrub_inline(str(servings))
        servings = re.sub(r"(?i)\bserves?\b", "", servings).strip(" :")

    table = TRNTable(
        title=title,
        ingredients=ingredients,
        steps=steps,
        servings=servings or None,
        source=draft.source,
        notes=scrub_inline(draft.notes) if draft.notes else "",
        id=str(uuid.uuid4()),
    )
    proteins = [i.name for i in ingredients if i.is_protein]
    if proteins:
        table.original_protein = proteins[0]
    return table
