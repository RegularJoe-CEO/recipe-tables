"""
Conversion quality gate.

Stops the app from presenting confident-looking garbage.
If the parse looks wrong, we say so clearly and push the user to edit
or use a cleaner input (paste / PDF of just the recipe card).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from .models import TRNTable
from .trn_engine import build_trn, MAX_ACTION_COLS
from .sanitize import assert_clean_output, is_junk_ingredient_line, is_junk_step


@dataclass
class QualityReport:
    ok: bool
    score: int  # 0–100
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            msg = f"Conversion looks usable (score {self.score}/100)."
            if self.warnings:
                msg += " Notes: " + "; ".join(self.warnings[:3])
            return msg
        return (
            f"Conversion looks WRONG (score {self.score}/100). "
            "Do not trust this TRN/PDF until you fix it in Edit mode "
            "or re-import cleaner text.\n\nProblems:\n- "
            + "\n- ".join(self.issues[:8])
        )


def assess_conversion(table: TRNTable) -> QualityReport:
    """
    Hard checks. Fail closed when the card would mislead the cook.
    """
    issues: List[str] = []
    warnings: List[str] = []
    score = 100

    title = (table.title or "").strip()
    if not title or title.lower() in {
        "untitled recipe",
        "empty recipe",
        "recipe",
        "sign in",
        "equipment",
        "recipes",
    }:
        issues.append(f"Bad title: “{title or '(empty)'}”")
        score -= 40

    if re.match(
        r"(?i)^(sign\s*in|log\s*in|equipment|ingredients?|directions?|nutrition|"
        r"related|you\s+may|more\s+recipes|newsletter|subscribe|print|share|"
        r"recipes?|home|comments?)\b",
        title,
    ):
        issues.append(f"Title looks like page chrome, not a dish name: “{title}”")
        score -= 35
    if re.search(r"(?i)\b(related recipes|you may also|sign in|subscribe)\b", title):
        issues.append(f"Title contains page chrome: “{title}”")
        score -= 30

    ings = table.ingredients or []
    if len(ings) < 2:
        issues.append(f"Too few ingredients ({len(ings)}) — parse probably failed")
        score -= 40
    elif len(ings) > 40:
        warnings.append(f"Very many ingredients ({len(ings)}); may include extras")
        score -= 5

    junk_ing = [
        i.display_left()
        for i in ings
        if is_junk_ingredient_line(i.display_left())
        or (i.name.isupper() and len(i.name) > 6)
        or re.search(r"&[a-z]=|\.jpg|w=\d+", i.name, re.I)
        or re.match(r"(?i)^(empalmes|fried |dried |makes |to serve)", i.name)
    ]
    if junk_ing:
        issues.append(
            "Junk/non-food ingredients: "
            + ", ".join(repr(x)[:40] for x in junk_ing[:4])
        )
        score -= 15 * min(3, len(junk_ing))

    # Quantity sanity: most ings should have amounts
    with_qty = sum(1 for i in ings if i.quantity.amount is not None or i.quantity.unit)
    if ings and with_qty < max(1, len(ings) // 3):
        issues.append("Most ingredients lack amounts — PDF spacing/parse likely broken")
        score -= 20

    steps = [s for s in (table.steps or []) if not s.is_header]
    headers = [s for s in (table.steps or []) if s.is_header]
    if len(steps) < 2:
        issues.append(f"Too few cooking steps ({len(steps)})")
        score -= 30

    # Giant "prep" blob = classic failure mode
    for h in headers:
        if len(h.detail or "") > 180:
            issues.append(
                "A Prep row contains almost the whole recipe — step splitting failed"
            )
            score -= 35
            break

    junk_steps = [
        s
        for s in table.steps or []
        if is_junk_step(s.detail or s.action or "")
        or re.search(
            r"(?i)\b(calories|provecho|sign in|newsletter|some people love)\b",
            s.detail or "",
        )
    ]
    if junk_steps:
        issues.append(f"{len(junk_steps)} junk/commentary step(s) still present")
        score -= 10 * min(3, len(junk_steps))

    # Action label quality via engine
    try:
        eng = build_trn(table)
        bad_labels = {
            "the",
            "a",
            "an",
            "these",
            "this",
            "about",
            "some",
            "when",
            "once",
            "slightly",
            "same",
            "use",
            "finished",
            "lightly",
            "component",
            "recipes",
            "equipment",
            "dr",
            "-inch",
        }
        bad = [m.action for m in eng.milestones if m.action.lower() in bad_labels]
        if bad:
            issues.append(f"Nonsense column labels: {bad[:6]}")
            score -= 15 * min(3, len(bad))
        if len(eng.milestones) > MAX_ACTION_COLS:
            issues.append(f"Too many TRN columns ({len(eng.milestones)})")
            score -= 10
        if len(eng.milestones) == 1 and len(steps) > 4:
            issues.append(
                "Only one TRN column but many steps — compression/split failed"
            )
            score -= 25
        for p in eng.prep:
            if len(p.detail or "") > 180:
                issues.append("Prep band is the whole recipe text")
                score -= 30
                break
    except Exception as e:
        issues.append(f"TRN engine error: {e}")
        score -= 40

    # Source pollution
    for field, val in (
        ("title", table.title),
        ("notes", table.notes or ""),
    ):
        probs = assert_clean_output(val or "", field)
        if probs:
            issues.append(f"Dirty {field}: {probs[0]}")
            score -= 10

    score = max(0, min(100, score))
    # Fail closed below 55
    ok = score >= 55 and not any(
        "Prep row contains" in i
        or "Bad title" in i
        or "Too few ingredients" in i
        or "Nonsense column" in i
        for i in issues
    )
    # If we have critical issues, never ok
    critical = [
        i
        for i in issues
        if any(
            k in i
            for k in (
                "Bad title",
                "page chrome",
                "Too few ingredients",
                "Prep row contains",
                "Prep band is the whole",
                "Only one TRN column",
                "Nonsense column",
            )
        )
    ]
    if critical:
        ok = False

    return QualityReport(ok=ok, score=score, issues=issues, warnings=warnings)
