"""
Universal TRN engine — one consistent set of rules for ANY recipe.

Philosophy
----------
Michael Chu's tables are not "one column per sentence". They are a
**dependency diagram**:

  • Left column: ingredients in **order of first use**
  • Each action column: a **milestone** where the working mixture changes
    (new ingredients join, or a major transform finishes)
  • Cell rowspan: ingredients currently in that mixture (solid block)
  • Prep (preheat / grease pan): full-width rows above the grid
  • Cap ~6 action columns so the card always fits a page

Pipeline (always the same, regardless of cuisine)
-------------------------------------------------
  1. Parse ingredients + steps (from trn_converter)
  2. Drop junk (notes, reviews, storage blurbs)
  3. Match ingredients → steps
  4. Compress steps into milestone columns
  5. Reorder ingredients by first-use column
  6. Build cascade spans (parallel streams vs main line)
  7. Validate (column count, labels, coverage)

This module owns steps 4–7 so every recipe path hits the same logic.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

from .models import Ingredient, Step, TRNTable

# Hard cap — CFE cards are wide, never 12+ skinny verb strips
MAX_ACTION_COLS = 6

# Verbs that open a meaningful TRN column
_MILESTONE_VERBS = re.compile(
    r"\b("
    r"melt|cream|whisk|beat|fold|sift|mix|combine|stir|toss|"
    r"bake|roast|simmer|sear|brown|fry|saute|sauté|grill|broil|boil|"
    r"blend|knead|whip|pour|assemble|marinate|chill|cook|heat|"
    r"season|garnish|serve|saute|sweat|reduce|braise|poach|steam|"
    r"stir-?fry|deep-?fry|pan-?fry"
    r")\b",
    re.I,
)

# Pure logistics — never their own column
_LOGISTICS = re.compile(
    r"\b("
    r"transfer|set\s+aside|reduce\s+(?:the\s+)?heat|remove\s+from\s+(?:the\s+)?(?:heat|pan|skillet)|"
    r"return\s+to|reserve|discard|meanwhile|repeat|pat\s+dry|"
    r"place\s+on\s+a\s+plate|move\s+to"
    r")\b",
    re.I,
)

_JUNK = re.compile(
    r"\b("
    r"recipe\s+notes?|storage\s*:|refrigerate\s+leftovers|reviews?\s+filed|"
    r"filed\s+in\s*:|print\s+recipe|jump\s+to|rate\s+this|nutrition|"
    r"calories\s*:|make\s+ahead|budget\s+\w+"
    r")\b",
    re.I,
)

# Leading cooking verb at start of instruction (not ingredient nouns mid-sentence)
_LEAD_VERB = re.compile(
    r"^\s*(?:meanwhile,?\s+|then,?\s+|next,?\s+|finally,?\s+)?"
    r"(?P<verb>"
    r"preheat|grease|butter\s+and\s+flour|line|spray|prepare|"
    r"melt|cream|whisk|beat|fold(?:\s+in)?|sift|mix(?:\s+in)?|combine|"
    r"stir(?:\s+in|-?fry)?|toss|bake|roast|simmer|sear|brown|fry|"
    r"saute|sauté|grill|broil|boil|blend|knead|whip|pour|assemble|"
    r"marinate|refrigerate|chill|smoke|cook|heat|season|garnish|serve|"
    r"add|place|pat|rub|transfer|return|remove|reduce|bring|cover|"
    r"uncover|drain|rinse|skim|strain|spoon|coat|roll|rise|let|"
    r"spread|top|sprinkle|drizzle|layer|arrange|cut|slice|chop|dice|"
    r"mince|peel|grate|zest|juice|toast|process|puree|purée|flour|grease"
    r")\b",
    re.I,
)


@dataclass
class Milestone:
    """One action column in the final TRN table."""

    action: str
    detail: str
    ingredient_ids: list[str] = field(default_factory=list)
    duration: str = ""
    is_header: bool = False


@dataclass
class EngineResult:
    """Everything the renderer/PDF need — stable for any recipe."""

    title: str
    servings: Optional[str]
    ingredients: list[Ingredient]  # reordered by first use
    prep: list[Milestone]  # full-width header rows
    milestones: list[Milestone]  # action columns (≤ MAX_ACTION_COLS)
    # per milestone: set of ingredient row indices (into `ingredients`)
    spans: list[set[int]]
    method_steps: list[Step]  # original full method (unmutated)
    notes: str = ""
    scale_factor: float = 1.0
    source: str = ""
    warnings: list[str] = field(default_factory=list)


def build_trn(table: TRNTable) -> EngineResult:
    """
    Main entry: TRNTable → EngineResult with universal layout rules applied.
    Never mutates the input table.
    """
    warnings: list[str] = []
    method_steps = list(table.steps)  # originals for Method prose

    # ── 1. Separate prep vs body; drop junk ──────────────────────────────
    prep_raw = [s for s in table.steps if s.is_header and not _is_junk(s)]
    body_raw = [s for s in table.steps if not s.is_header and not _is_junk(s)]

    if not body_raw and not prep_raw:
        warnings.append("No usable steps found")
        n = max(len(table.ingredients), 1)
        return EngineResult(
            title=table.title,
            servings=table.servings,
            ingredients=list(table.ingredients),
            prep=[],
            milestones=[Milestone(action="prepare", detail="")],
            spans=[set(range(n))],
            method_steps=method_steps,
            notes=table.notes,
            scale_factor=table.scale_factor or 1.0,
            source=table.source,
            warnings=warnings,
        )

    # ── 2. Compress body steps into milestones ───────────────────────────
    milestones = _milestones_from_steps(body_raw, warnings)
    prep = [
        Milestone(
            action=_lead_action(s),
            detail=s.detail or s.action,
            is_header=True,
        )
        for s in prep_raw
    ]

    # ── 3. Reorder ingredients by first use across milestones ────────────
    ingredients, id_remap = _reorder_by_first_use(table.ingredients, milestones)

    # Remap milestone ingredient ids is unnecessary if we keep same Ingredient objects
    id_to_row = {ing.id: i for i, ing in enumerate(ingredients)}

    # ── 4. Cascade spans ─────────────────────────────────────────────────
    spans = _cascade_spans(milestones, id_to_row, len(ingredients))

    # ── 5. Validate / repair ─────────────────────────────────────────────
    milestones, spans = _validate_and_repair(
        milestones, spans, len(ingredients), warnings
    )

    return EngineResult(
        title=table.title,
        servings=table.servings,
        ingredients=ingredients,
        prep=prep,
        milestones=milestones,
        spans=spans,
        method_steps=method_steps,
        notes=table.notes,
        scale_factor=table.scale_factor or 1.0,
        source=table.source,
        warnings=warnings,
    )


# ── milestones ───────────────────────────────────────────────────────────────

def _milestones_from_steps(body: list[Step], warnings: list[str]) -> list[Milestone]:
    """
    Convert N instruction sentences into ≤ MAX_ACTION_COLS milestones.

    A new milestone opens when:
      • new ingredients appear that the previous milestone didn't use, OR
      • a major timed transform happens (bake/simmer/cook N min)

    Logistics and pure follow-up cook sentences fold into the current milestone.
    """
    if not body:
        return []

    # Working list of milestones (mutable)
    ms: list[Milestone] = []
    seen_ings: set[str] = set()

    for step in body:
        action = _lead_action(step)
        ings = list(step.ingredient_ids or [])
        new_ings = [i for i in ings if i not in seen_ings]
        is_log = _is_logistics(step, action)
        detail_l = (step.detail or "").lower()
        has_major_time = bool(
            (step.duration or re.search(r"\b\d+\s*(?:to\s*\d+\s*)?(?:minutes?|hours?|mins?)\b", detail_l))
            and re.search(
                r"\b(bake|simmer|roast|cook|chill|marinate|boil|smoke|rise)\b",
                f"{action} {detail_l}",
                re.I,
            )
        )

        if is_log and ms:
            # Fold logistics into previous milestone (detail only)
            ms[-1].detail = _join_detail(ms[-1].detail, step.detail)
            if step.duration and not ms[-1].duration:
                ms[-1].duration = step.duration
            continue

        if not ms:
            ms.append(
                Milestone(
                    action=action,
                    detail=step.detail or "",
                    ingredient_ids=list(ings),
                    duration=step.duration or "",
                )
            )
            seen_ings.update(ings)
            continue

        # Variation / live-fire / section banners / chatter never become milestones
        if re.search(
            r"(?i)\b(live fire|campfire|as in the book|can be made over|"
            r"proceed with the recipe as written|adjust the cook time|"
            r"embers|wood or charcoal|grate over the embers|"
            r"provecho|some people love|go ahead and season|"
            r"set aside to proceed|calories|carbohydrates)\b",
            step.detail or "",
        ):
            continue
        if re.match(
            r"^(?i)(empalmes?|fried pinto|dried piquin|about \d+ minutes|"
            r"do the same with|\[component\])\b",
            step.detail or "",
        ):
            continue
        if action.lower() in {
            "about",
            "some",
            "these",
            "empalmes",
            "fried",
            "dried",
            "same",
            "once",
            "slightly",
        }:
            continue

        # Overnight marinate is NEVER absorbed into the next heat/smoke step
        is_marinate = action.lower() in {"marinate", "refrigerate", "rub"} or (
            re.search(r"\bovernight\b", (step.detail or "").lower())
            and re.search(
                r"\b(refrigerate|marinate|rub|coat)\b",
                (step.detail or "").lower(),
            )
        )
        is_smoke_phase = action.lower() in {"smoke", "grill"} or (
            re.search(r"\bsmoke\b", (step.detail or "").lower())
            and re.search(r"\b(hour|hours|minutes?)\b", (step.detail or "").lower())
        )

        # Same working bowl, no new ings, weak follow-up → absorb
        # Do NOT absorb marinate into smoke, or smoke into pour/braise
        absorb = (
            not new_ings
            and action.lower()
            in {
                "add",
                "cook",
                "stir",
                "toss",
                "heat",
                "place",
                "season",
                "pat",
                "continue",
                "remove",
                "transfer",
                "return",
                "cut",
                "roll",
                "arrange",
                "cover",
                "let",
                "rise",
                "flour",  # "lightly flour work surface"
                "use",
            }
            and not has_major_time
            and not is_marinate
            and not is_smoke_phase
            and ms[-1].action.lower() not in {"marinate", "smoke", "bake"}
        )
        # Shaping steps after mix: roll/cut/arrange can merge into one "shape" column
        if (
            ms
            and action.lower() in {"roll", "cut", "arrange", "flour", "pat"}
            and ms[-1].action.lower() in {"roll", "cut", "shape", "flour", "pat", "mix", "add"}
            and not has_major_time
        ):
            if ms[-1].action.lower() in {"mix", "add"}:
                # start shaping as new milestone with better label
                action = "shape" if action.lower() in {"roll", "cut", "flour", "pat"} else action
            else:
                absorb = True
                action = "shape"
        # heat smoker + place lamb can fold into upcoming smoke
        if (
            ms
            and action.lower() in {"heat", "place"}
            and not new_ings
            and re.search(r"\b(smoker|grill|grates)\b", (step.detail or "").lower())
        ):
            # hold for merge into smoke — open milestone only if no smoke yet
            if ms[-1].action.lower() in {"heat", "place", "smoke"}:
                absorb = True
            else:
                absorb = False

        if absorb:
            prev = ms[-1]
            # Never rename a marinate column to something else by absorbing
            if prev.action.lower() == "marinate" and action.lower() != "marinate":
                # close marinate; start new milestone below
                pass
            else:
                prev.detail = _join_detail(prev.detail, step.detail)
                prev.ingredient_ids = list(
                    dict.fromkeys(prev.ingredient_ids + ings)
                )
                if step.duration and (
                    not prev.duration or len(step.duration) > len(prev.duration)
                ):
                    prev.duration = step.duration
                # Upgrade weak labels: melt+cook → cook; heat+smoke → smoke
                if action.lower() in {
                    "cook",
                    "sear",
                    "brown",
                    "simmer",
                    "smoke",
                } and prev.action.lower() in {
                    "add",
                    "melt",
                    "heat",
                    "place",
                }:
                    prev.action = action
                if action.lower() == "smoke":
                    prev.action = "smoke"
                continue

        # If previous was heat/place into smoker and this is smoke → merge forward label
        if (
            ms
            and is_smoke_phase
            and ms[-1].action.lower() in {"heat", "place"}
            and not re.search(r"\bovernight\b", ms[-1].detail.lower())
        ):
            prev = ms[-1]
            prev.action = "smoke"
            prev.detail = _join_detail(prev.detail, step.detail)
            prev.ingredient_ids = list(dict.fromkeys(prev.ingredient_ids + ings))
            if step.duration:
                prev.duration = step.duration
            seen_ings.update(ings)
            continue

        # Open a new milestone
        # Merge combine+rub overnight into one marinate column
        if (
            ms
            and is_marinate
            and ms[-1].action.lower() in {"mix", "combine", "season", "rub"}
        ):
            prev = ms[-1]
            prev.action = "marinate"
            prev.detail = _join_detail(prev.detail, step.detail)
            prev.ingredient_ids = list(dict.fromkeys(prev.ingredient_ids + ings))
            seen_ings.update(ings)
            continue

        ms.append(
            Milestone(
                action=action if not is_marinate else "marinate",
                detail=step.detail or "",
                ingredient_ids=list(ings),
                duration=step.duration or "",
            )
        )
        seen_ings.update(ings)

    # Hard cap: merge lowest-value adjacent pairs
    while len(ms) > MAX_ACTION_COLS:
        best_i, best_score = 0, 10**9
        for i in range(len(ms) - 1):
            score = _importance(ms[i]) + _importance(ms[i + 1])
            if score < best_score:
                best_score = score
                best_i = i
        a, b = ms[best_i], ms[best_i + 1]
        keep = a if _importance(a) >= _importance(b) else b
        other = b if keep is a else a
        combined = Milestone(
            action=keep.action,
            detail=_join_detail(a.detail, b.detail),
            ingredient_ids=list(dict.fromkeys(a.ingredient_ids + b.ingredient_ids)),
            duration=keep.duration or other.duration,
        )
        # Prefer timed major verb as label when merging
        if other.duration and not keep.duration:
            combined.action = other.action
            combined.duration = other.duration
        ms = ms[:best_i] + [combined] + ms[best_i + 2 :]
        warnings.append(f"Merged columns to fit ≤{MAX_ACTION_COLS}")

    if not ms:
        ms = [Milestone(action="prepare", detail="")]
    return ms


def _importance(m: Milestone) -> int:
    score = 0
    score += min(4, len(m.ingredient_ids))
    if _MILESTONE_VERBS.search(m.action):
        score += 3
    if m.duration:
        score += 2
    if m.action.lower() in {"add", "place", "heat", "pat", "transfer"}:
        score -= 2
    return score


def _is_logistics(step: Step, action: str) -> bool:
    act = action.lower()
    if act in {
        "transfer",
        "return",
        "remove",
        "reserve",
        "discard",
        "repeat",
        "set aside",
    }:
        return True
    # "Reduce the heat to low" with no new work
    if act in {"heat", "reduce"} and not step.ingredient_ids:
        if re.search(r"reduce\s+(?:the\s+)?heat|heat\s+to\s+low", step.detail or "", re.I):
            return True
    if _LOGISTICS.search(step.detail or "") and not step.ingredient_ids:
        return True
    return False


def _is_junk(step: Step) -> bool:
    t = f"{step.action} {step.detail}"
    return bool(_JUNK.search(t)) or bool(re.match(r"^\s*reviews?\b", step.detail or "", re.I))


def _lead_action(step: Step) -> str:
    """
    Extract the cooking verb from the START of the instruction.
    Never match ingredient nouns mid-sentence (e.g. 'cream' in 'heavy cream').

    Special cases (BBQ / make-ahead):
      • "Rub … refrigerate overnight" → marinate
      • "Smoke for 3 hours" → smoke
      • Never let a later smoke step steal the marinate label
    """
    text = (step.detail or step.action or "").strip()
    detail_l = text.lower()

    # Overnight / make-ahead marinate is its own milestone — label it clearly
    if re.search(
        r"\b(refrigerate|marinate|rest)\b.*\b(overnight|several hours|hours?|"
        r"covered)\b|\bovernight\b",
        detail_l,
    ) and not re.search(r"\bsmoke\b", detail_l):
        if re.search(r"\b(rub|coat|combine|mix|season)\b", detail_l) or re.search(
            r"\b(refrigerate|marinate)\b", detail_l
        ):
            return "marinate"

    # Normalize "stir -fry"
    text = re.sub(r"\b([A-Za-z]+)\s*-\s*([A-Za-z]+)\b", r"\1-\2", text)
    # Strip adverbs / temporal openers so "Lightly flour" → flour, "When finished, roll" → roll
    text = re.sub(
        r"^(?:lightly|gently|carefully|slowly|quickly|then|next|finally|"
        r"when finished,?|once done,?|after that,?|slightly|"
        r"once the oil is hot,?|once hot,?)\s+",
        "",
        text,
        flags=re.I,
    )
    # "Use a 2½-inch biscuit cutter to cut…" → start at "cut"
    text = re.sub(
        r"^use\s+a\s+[\d\s/½¼¾.\-inch]+(?:\s+\w+){0,4}\s+to\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^use\s+a\s+\w+(?:\s+\w+){0,3}\s+to\s+", "", text, flags=re.I)
    # "In a bowl, toss the chicken…" → skip prepositional opener
    text = re.sub(
        r"^(?:in\s+a\s+\w+|in\s+the\s+\w+|using\s+a\s+\w+|with\s+a\s+\w+),?\s+",
        "",
        text,
        flags=re.I,
    )

    m = _LEAD_VERB.match(text)
    if m:
        verb = re.sub(r"\s+", " ", m.group("verb").lower())
        # Canonical short CFE labels
        canon = {
            "fold in": "fold in",
            "mix in": "mix",
            "stir in": "stir in",
            "stir-fry": "stir-fry",
            "beat in": "beat",
            "whisk in": "whisk",
            "butter and flour": "butter and flour",
            "set aside": "set aside",
            "saute": "sauté",
            "combine": "mix",
            "refrigerate": "marinate",
            "rub": "rub",
            "smoke": "smoke",
        }
        return canon.get(verb, verb)

    # Scan for first real cooking verb after openers (still not mid-ingredient)
    m2 = re.search(
        r"\b(?P<verb>"
        r"melt|cream|whisk|beat|fold|sift|mix|combine|stir-?fry|stir|"
        r"toss|bake|roast|simmer|sear|brown|smoke|cook|heat|season|garnish|"
        r"serve|pour|add|saute|sauté|grill|boil|whip|assemble|marinate|"
        r"refrigerate|rub|skim|strain|coat|spoon"
        r")\b",
        text,
        re.I,
    )
    if m2:
        v = m2.group("verb").lower().replace("stirfry", "stir-fry")
        if v == "refrigerate":
            return "marinate"
        return v

    # Never use articles / weak words as column labels
    stop = {
        "in", "a", "an", "the", "to", "of", "and", "or", "for", "with",
        "your", "my", "it", "its", "this", "that", "them", "they", "these",
        "those", "use",  # "use a whisk" / "use a knife" — tool phrase
        "using", "when", "before", "after", "while",
    }
    # Fall back to precomputed short action if it looks like a verb
    if (
        step.action
        and len(step.action) <= 20
        and step.action.lower() not in stop
    ):
        return step.action.lower()
    words = re.findall(r"[a-zA-Z\-]+", text)
    for w in words:
        if w.lower() not in stop and len(w) > 2:
            return w.lower()[:20]
    return "step"


def _join_detail(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    if not a:
        return b
    if not b or b in a:
        return a
    if a in b:
        return b
    return f"{a} {b}".strip()


# ── ingredient order ─────────────────────────────────────────────────────────

def _reorder_by_first_use(
    ingredients: list[Ingredient],
    milestones: list[Milestone],
) -> tuple[list[Ingredient], dict[str, int]]:
    """
    CFE lists ingredients in order of first use so rowspans form solid blocks.
    Ingredients never mentioned in steps keep their relative order at the end.
    """
    first: dict[str, int] = {}
    for col, m in enumerate(milestones):
        for iid in m.ingredient_ids:
            if iid not in first:
                first[iid] = col

    indexed = list(enumerate(ingredients))

    def sort_key(item: tuple[int, Ingredient]):
        orig_i, ing = item
        return (first.get(ing.id, 10_000), orig_i)

    ordered = [ing for _, ing in sorted(indexed, key=sort_key)]
    remap = {ing.id: i for i, ing in enumerate(ordered)}
    return ordered, remap


# ── cascade ──────────────────────────────────────────────────────────────────

def _cascade_spans(
    milestones: list[Milestone],
    id_to_row: dict[str, int],
    n_ing: int,
) -> list[set[int]]:
    """
    For each milestone, decide which ingredient rows the action cell covers.

    Rules (same for every recipe):
      • direct = rows named in this milestone
      • If direct is non-empty, disjoint from active, and action is a 'prep
        stream' verb (melt/heat/whisk dry/…) → parallel (span = direct only)
      • Else → span = active ∪ direct (solid min..max), then active ← span
      • If nothing tracked yet and no direct → span all rows (rare fallback)
    """
    PREP_STREAM = re.compile(
        r"\b(melt|heat|whisk|sift|combine|mix|cream|beat|chop|dice|slice|mince)\b",
        re.I,
    )
    JOIN = re.compile(
        r"\b("
        r"fold|stir\s*in|mix\s*in|add|combine|toss|pour|return|bake|simmer|"
        r"cook|serve|garnish|assemble|transfer|top"
        r")\b",
        re.I,
    )

    spans: list[set[int]] = []
    active: set[int] = set()
    # Track parallel products so a later join can union them
    parallel_products: list[set[int]] = []

    for m in milestones:
        direct = {id_to_row[i] for i in m.ingredient_ids if i in id_to_row}
        brand_new = bool(direct) and direct.isdisjoint(active)
        is_join = bool(JOIN.search(m.action) or JOIN.search(m.detail))
        is_prep_stream = bool(PREP_STREAM.search(m.action)) and not is_join

        # "return the chicken" / action names an old + new → join
        if direct and active and not direct.isdisjoint(active):
            is_join = True

        parallel = brand_new and is_prep_stream and bool(active) and not is_join

        if parallel:
            rows = set(direct)
            parallel_products.append(set(rows))
        else:
            rows = set(direct) | set(active)
            # Absorb open parallel streams when joining (e.g. add chicken to melted butter)
            if is_join or (direct and active):
                for pp in parallel_products:
                    if direct & pp or active & pp or not direct.isdisjoint(pp):
                        rows |= pp
                # If adding something into a pan that had a parallel melt, union all parallels
                if is_join and parallel_products:
                    for pp in parallel_products:
                        rows |= pp
                    parallel_products.clear()
            if not rows:
                rows = set(active) if active else set(range(max(n_ing, 1)))
            # Solid block
            lo, hi = min(rows), max(rows)
            rows = set(range(lo, hi + 1))
            active = set(rows)

        spans.append(rows)

    return spans


# ── validate ─────────────────────────────────────────────────────────────────

def _validate_and_repair(
    milestones: list[Milestone],
    spans: list[set[int]],
    n_ing: int,
    warnings: list[str],
) -> tuple[list[Milestone], list[set[int]]]:
    """Guarantee invariants the PDF/HTML can rely on."""
    # Non-empty labels
    for m in milestones:
        if not (m.action or "").strip():
            m.action = "mix"
            warnings.append("Empty action label repaired to 'mix'")

    # Column cap (should already hold)
    if len(milestones) > MAX_ACTION_COLS:
        warnings.append(f"Still over column cap ({len(milestones)}); forcing merge")
        while len(milestones) > MAX_ACTION_COLS:
            a, b = milestones[-2], milestones[-1]
            a.detail = _join_detail(a.detail, b.detail)
            a.ingredient_ids = list(dict.fromkeys(a.ingredient_ids + b.ingredient_ids))
            a.duration = a.duration or b.duration
            spans[-2] = spans[-2] | spans[-1]
            if spans[-2]:
                lo, hi = min(spans[-2]), max(spans[-2])
                spans[-2] = set(range(lo, hi + 1))
            milestones.pop()
            spans.pop()

    # Every span non-empty
    for i, sp in enumerate(spans):
        if not sp:
            spans[i] = {0} if n_ing else set()
            warnings.append(f"Empty span on column {i} repaired")

    # Align lengths
    while len(spans) < len(milestones):
        spans.append(set(range(n_ing)) if n_ing else set())
    spans = spans[: len(milestones)]

    return milestones, spans


def action_label(m: Milestone) -> str:
    """Display label for a milestone cell (bake gets temp/time lines)."""
    label = (m.action or "step").strip()
    detail = m.detail or ""
    if re.search(r"\bbake\b", label, re.I) or re.search(r"\bbake\b", detail, re.I):
        parts = ["bake"]
        tm = re.search(
            r"(\d+\s*°\s*[FC](?:\s*\(\s*\d+\s*°\s*[FC]\s*\))?)",
            detail,
            re.I,
        )
        if tm:
            parts.append(re.sub(r"\s+", "", tm.group(1)))
        if m.duration:
            dur = m.duration
            dur = re.sub(r"\s*minutes?\b", " min", dur, flags=re.I)
            dur = re.sub(r"\s*mins?\b", " min", dur, flags=re.I)
            dur = re.sub(r"\s*[–—\-]\s*", " to ", dur)
            parts.append(dur)
        return "\n".join(parts)
    if m.duration and label.lower() in {
        "chill",
        "rest",
        "marinate",
        "simmer",
        "cook",
        "boil",
        "sear",
        "brown",
    }:
        dur = re.sub(r"\s*minutes?\b", " min", m.duration, flags=re.I)
        return f"{label}\n{dur}"
    return label
