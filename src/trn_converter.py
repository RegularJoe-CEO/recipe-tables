"""
Core converter: free-form recipe → Tabular Recipe Notation (TRN).

Conversion strategy (robust heuristic, not ML):
  1. Split text into TITLE / INGREDIENTS / INSTRUCTIONS sections.
  2. Parse each ingredient line into structured Quantity + name.
  3. Parse instructions into ordered steps; associate ingredients that
     appear (by name substring) in each step.
  4. Detect whole-dish prep actions (preheat, grease, line pan…) as
     header steps so they sit above the ingredient grid.
  5. Build consumes_steps edges when a later step refers to a prior
     mixture ("add batter", "fold in", "combine", "pour into").

The renderer (trn_renderer / pdf_exporter) uses ingredient_ids +
consumes_steps to draw rowspan-style combination cells.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import Ingredient, Step, TRNTable
from .quantity_parser import parse_ingredient_line
from .sanitize import (
    sanitize_recipe_text,
    sanitize_title,
    is_junk_ingredient_line,
    is_junk_step,
    scrub_inline,
)
from .text_cleanup import cleanup_recipe_text, is_non_ingredient_line

# Section headers commonly found in recipe text
ING_HEADERS = re.compile(
    r"^(ingredients?|what\s+you(?:['’]ll|\s+will)\s+need|shopping\s+list)\s*:?\s*$",
    re.I | re.M,
)
STEP_HEADERS = re.compile(
    r"^(instructions?|directions?|method|steps?|preparation|how\s+to\s+make(?:\s+it)?)\s*:?\s*$",
    re.I | re.M,
)
# Only real yield lines — not "serving hot" mid-instruction
SERVINGS_RE = re.compile(
    r"(?:^|\n)\s*(?:serves?|servings?|yield|makes?)\s*:?\s*"
    r"((?:approximately\s+|about\s+)?\d+\s*(?:to\s*\d+)?\s*"
    r"(?:side\s+)?(?:people|servings?|portions?)?"
    r"|\d+\s*[-–]\s*\d+"
    r"|[^\n]{1,40})",
    re.I,
)

# Whole-dish prep → full-width header rows at top of TRN table
# (matches CFE cards: "Butter and flour an 8x8-in pan", "Preheat oven…")
HEADER_ACTIONS = re.compile(
    r"\b("
    r"preheat|heat\s+(?:your\s+|the\s+)?oven|heat\s+oven|"
    r"grease|"
    r"butter\s+and\s+flour|"
    r"butter\s+(?:and\s+flour\s+)?(?:an?\s+)?(?:the\s+)?(?:[\d\s./x×-]+(?:-?\s*in(?:ch(?:es)?)?\s+)?)?(?:pan|dish|skillet|casserole|baking)|"
    r"flour\s+(?:an?\s+)?(?:\d|the\s+)?(?:pan|dish)|"
    r"line\s+(?:the\s+)?(?:pan|baking|sheet)|"
    r"spray\s+(?:with\s+)?(?:cooking|non[- ]stick)|"
    r"prepare\s+(?:a\s+)?(?:baking|sheet|pan)|"
    r"set\s+(?:aside\s+)?(?:a\s+)?(?:rack|sheet)|"
    r"bring\s+(?:a\s+)?(?:large\s+)?pot\s+of\s+water"
    r")\b",
    re.I,
)

# Verbs that often start a cooking step (include hyphenated forms like stir-fry)
ACTION_START = re.compile(
    r"^(?P<verb>"
    r"mix|combine|whisk|beat|stir-?fry|stir|fold|cream|sift|melt|heat|saute|sauté|"
    r"cook|bake|roast|broil|boil|simmer|deep-?fry|pan-?fry|fry|sear|brown|grill|steam|"
    r"chop|dice|mince|slice|cut|peel|grate|zest|juice|"
    r"add|pour|spread|transfer|place|arrange|layer|"
    r"cover|uncover|reduce|increase|season|taste|serve|garnish|"
    r"drain|rinse|pat|dry|toast|blend|puree|purée|process|"
    r"marinate|rest|chill|refrigerate|freeze|thaw|reheat|"
    r"knead|roll|press|brush|drizzle|top|sprinkle|dust|"
    r"remove|discard|reserve|set\s+aside|let\s+(?:cool|rest|sit)|"
    r"preheat|grease|line|spray|prepare"
    r")\b",
    re.I,
)

# Phrases indicating combination of prior mixtures
COMBINE_PHRASES = re.compile(
    r"\b("
    r"combine|mix\s+together|fold\s+(?:in|into)|stir\s+in|"
    r"add\s+(?:the\s+)?(?:batter|mixture|dough|sauce|marinade|broth|stock)|"
    r"pour\s+(?:the\s+)?(?:batter|mixture|sauce)|"
    r"transfer\s+(?:to|into)|return\s+to|"
    r"toss\s+(?:together|with)|bring\s+together"
    r")\b",
    re.I,
)

PROTEIN_WORDS = re.compile(
    r"\b("
    r"chicken|beef|pork|turkey|tofu|tempeh|shrimp|prawns?|salmon|cod|tuna|"
    r"lamb|bacon|sausage|seitan|steak|ham|duck|venison|fish|seafood|"
    r"ground\s+(?:beef|turkey|chicken|pork)|chickpeas?|black\s+beans?"
    r")\b",
    re.I,
)


def convert_text_to_trn(
    text: str,
    title: Optional[str] = None,
    source: str = "pasted text",
) -> TRNTable:
    """
    Main entry: raw recipe text → TRNTable.

    Always runs the sanitizer first so ad/URL/visit junk never becomes
    ingredients or steps.
    """
    text = _normalize_text(text)
    text = cleanup_recipe_text(text)
    text, _sanitize_warnings = sanitize_recipe_text(text)
    if not text.strip():
        return TRNTable(title=sanitize_title(title or "Empty Recipe"), source=source)

    detected_title = sanitize_title(title or _extract_title(text))
    servings = _extract_servings(text)
    # Clean "Serves Serves approximately 6 people 1x" → "6"
    if servings:
        servings = scrub_inline(servings)
        servings = re.sub(r"(?i)\bserves?\s+", "", servings).strip()
        servings = re.sub(r"\b[123]x\b", "", servings, flags=re.I).strip()
        servings = re.sub(r"\s{2,}", " ", servings).strip(" :")

    ing_block, step_block = _split_sections(text)

    ingredients = _parse_ingredients(ing_block)
    # Mark proteins
    for ing in ingredients:
        if PROTEIN_WORDS.search(ing.name):
            ing.is_protein = True

    notes: list[str] = []
    steps = _parse_steps(step_block, ingredients, notes)

    # If no structured ingredients were found, invent rows from step mentions
    if not ingredients:
        ingredients = _infer_ingredients_from_steps(step_block)
        notes = []
        steps = _parse_steps(step_block, ingredients, notes)

    # Drop junk steps (ads, visit, storage blurbs, reviews)
    steps = [s for s in steps if not _is_junk_step(s) and not is_junk_step(s.detail or s.action)]

    # Scrub any URL/visit residue that slipped into fields
    for ing in ingredients:
        ing.name = scrub_inline(ing.name)
    for s in steps:
        s.action = scrub_inline(s.action)
        s.detail = scrub_inline(s.detail)

    # Drop ingredients that became empty or junk after scrub
    # Dedupe ingredients by normalized name (PDF salvage can double tortillas)
    seen_names: set[str] = set()
    deduped: list[Ingredient] = []
    for i in ingredients:
        if not i.name.strip() or is_junk_ingredient_line(i.name):
            continue
        key = re.sub(r"\s+", " ", i.name.lower().strip())
        if key in seen_names:
            continue
        seen_names.add(key)
        deduped.append(i)
    ingredients = deduped

    table = TRNTable(
        title=detected_title,
        ingredients=ingredients,
        steps=steps,
        servings=servings or None,
        source=source,
        notes=" ".join(notes),
    )
    # Record original protein for substitution feature
    proteins = [i.name for i in ingredients if i.is_protein]
    if proteins:
        table.original_protein = proteins[0]
    return table


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    # Strip per-line indent so "    Ingredients" still matches section headers
    # (common when recipes are pasted from indented sources or test strings)
    text = "\n".join(line.strip() for line in text.split("\n"))
    # Collapse 3+ newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_title(text: str) -> str:
    """
    First real title line — skip ratings, bylines, nav, and section headers.
    """
    skip_meta = re.compile(
        r"^(?:"
        r"by\s+\w+|written\s+by|recipe\s+by|from\s+\w+|"
        r"updated|published|posted|"
        r"\d+(\.\d+)?\s*(from|stars?|ratings?)|"
        r"(prep|cook|total)\s*time|"
        r"\d+\s*(servings?|mins?|minutes?|hrs?|hours?|kcal)|"
        r"jump\s+to|print|share|save|pin|"
        r"serves?\s*:|servings?\s*:|yield\s*:|makes?\s*:"
        r")\b",
        re.I,
    )
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if ING_HEADERS.match(s) or STEP_HEADERS.match(s):
            continue
        if re.match(r"^[\-\*\u2022•\d]", s):
            continue
        if skip_meta.match(s):
            continue
        if len(s) < 3:
            continue
        if len(s) < 100:
            return s
        return s[:80].rstrip(".,;:") + ("…" if len(s) > 80 else "")
    return "Untitled Recipe"


def _extract_servings(text: str) -> Optional[str]:
    # Prefer a yield line in the first ~15 lines (header area)
    head = "\n".join(text.split("\n")[:20])
    m = SERVINGS_RE.search(head) or SERVINGS_RE.search(text)
    if not m:
        return None
    s = m.group(1).strip().rstrip(".")
    # Reject instruction fragments accidentally captured
    if re.search(r"(?i)\b(hot|until|minutes?|skillet|pan|oven|add|stir)\b", s):
        # keep only if it still has a number + people/servings
        if not re.search(r"\d", s):
            return None
        if re.search(r"(?i)\b(hot|until|skillet|add|stir)\b", s):
            return None
    s = re.sub(r"(?i)\bserves?\b", "", s).strip()
    s = re.sub(r"\b[123]x\b", "", s, flags=re.I).strip()
    s = re.sub(r"\s{2,}", " ", s).strip(" :,-")
    return s or None


def _split_sections(text: str) -> tuple[str, str]:
    """Return (ingredients_block, steps_block)."""
    ing_m = ING_HEADERS.search(text)
    step_m = STEP_HEADERS.search(text)

    if ing_m and step_m:
        if ing_m.start() < step_m.start():
            ing_block = text[ing_m.end() : step_m.start()].strip()
            step_block = text[step_m.end() :].strip()
            # Magazine PDFs often do:
            #   Ingredients
            #   Directions
            #   2 cups flour   ← food list under Directions, empty Ingredients
            if len(ing_block) < 15 or not re.search(r"\d", ing_block):
                return _split_qty_lines_from_prose(step_block)
            return ing_block, step_block
        # Steps listed first (unusual)
        return text[ing_m.end() :], text[step_m.end() : ing_m.start()]

    if ing_m and not step_m:
        # Everything after ingredients; try to split on blank line + verb
        after = text[ing_m.end() :]
        return _heuristic_split(after)

    if step_m and not ing_m:
        after = text[step_m.end() :].strip()
        before = text[: step_m.start()]
        # If "Directions" is followed by qty lines, split qty vs prose
        if re.match(r"^[\d½¼¾⅓⅔1-9]", after, re.M):
            return _split_qty_lines_from_prose(after)
        return before, after

    # No headers — heuristic split on double newline
    parts = re.split(r"\n\s*\n", text, maxsplit=1)
    if len(parts) == 2:
        # Prefer the denser list-like block as ingredients
        a, b = parts
        a_list = sum(1 for ln in a.split("\n") if re.match(r"^[\-\*\u2022•\d]", ln.strip()))
        b_list = sum(1 for ln in b.split("\n") if re.match(r"^[\-\*\u2022•\d]", ln.strip()))
        if a_list >= b_list:
            return a, b
        return b, a
    return _split_qty_lines_from_prose(text)


def _split_qty_lines_from_prose(block: str) -> tuple[str, str]:
    """
    Split a block that mixes ingredient qty-lines and instruction prose:
      2 cups flour
      1 cup masa
      Heat your oven...
      Add the buttermilk...
    """
    lines = block.split("\n")
    ing_lines: list[str] = []
    step_start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            if step_start is None:
                continue
            # blank inside steps
            continue
        if STEP_HEADERS.match(s) or ING_HEADERS.match(s):
            continue
        # Instruction starts: cooking verb, or numbered step
        if re.match(r"^\d+[\.\)]\s+\w+", s):
            step_start = i
            break
        if ACTION_START.match(s) or HEADER_ACTIONS.search(s):
            step_start = i
            break
        if re.match(
            r"^(heat|preheat|in a |using |combine|whisk|mix|add|fold|bake|pat|place|serve)\b",
            s,
            re.I,
        ):
            step_start = i
            break
        # Qty / ingredient line
        if re.match(r"^[\d½¼¾⅓⅔⅛]|to taste|a pinch", s, re.I) or re.search(
            r"\b(cup|tbsp|tsp|oz|lb|g|ml|clove|tablespoon|teaspoon|stick)\b",
            s,
            re.I,
        ):
            ing_lines.append(s)
            continue
        # Continuation of previous ingredient (rare after cleanup)
        if ing_lines and not re.match(r"^[A-Z]", s):
            ing_lines[-1] = ing_lines[-1] + " " + s
            continue
        # Otherwise treat as start of prose if we already have ings
        if ing_lines:
            step_start = i
            break
        ing_lines.append(s)

    if step_start is None:
        # half-half fallback
        if len(ing_lines) >= 2:
            return "\n".join(ing_lines), ""
        return _heuristic_split(block)

    return "\n".join(ing_lines), "\n".join(lines[step_start:])


def _heuristic_split(block: str) -> tuple[str, str]:
    lines = block.split("\n")
    cut = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            # Look ahead for instruction-like line
            for j in range(i + 1, min(i + 4, len(lines))):
                if ACTION_START.match(lines[j].strip()) or re.match(
                    r"^\d+[\.\)]\s+\w+", lines[j].strip()
                ):
                    cut = i
                    break
        if cut is not None:
            break
        if re.match(r"^\d+[\.\)]\s+", s) and i > 2:
            cut = i
            break
    if cut is None:
        # Half-and-half fallback
        mid = max(1, len(lines) // 2)
        return "\n".join(lines[:mid]), "\n".join(lines[mid:])
    return "\n".join(lines[:cut]), "\n".join(lines[cut:])


def _parse_ingredients(block: str) -> list[Ingredient]:
    ingredients: list[Ingredient] = []
    for raw in block.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if ING_HEADERS.match(line) or STEP_HEADERS.match(line):
            continue
        if is_non_ingredient_line(line):
            continue
        # Skip pure section notes
        if re.match(r"^(for the|optional)\b", line, re.I) and ":" in line:
            continue
        if len(line) > 200:
            continue
        # Hard reject ad/URL/visit garbage before parsing
        if is_junk_ingredient_line(line):
            continue
        line = scrub_inline(line)
        # Strip any leftover glued "Directions " prefix
        line = re.sub(
            r"(?i)^(?:ingredients?|directions?|instructions?|method)\s+",
            "",
            line,
        ).strip()
        if not line or is_junk_ingredient_line(line) or is_non_ingredient_line(line):
            continue
        # Instruction sentences are NOT ingredients (even if they mention oil/salt)
        if re.match(
            r"(?i)^(combine|mix|add|heat|cook|bake|stir|fold|pour|place|remove|"
            r"transfer|rub|smoke|serve|whisk|season|refrigerate|marinate|"
            r"pat|cover|return|simmer|bring|skim|strain|coat|spoon)\b",
            line,
        ):
            continue
        # Dish titles ("Dr Pepper-Glazed Lamb Shoulder") — no leading qty
        if not re.match(
            r"^[\d½¼¾⅓⅔⅛⅜⅝⅞]|to taste|a pinch|a few|juice of|zest of",
            line,
            re.I,
        ):
            # bare "salt and pepper" ok; multi-word glazed/roasted dish names not
            if re.search(
                r"(?i)\b(glazed|smoked|roasted|grilled|braised|fried|baked|"
                r"shoulder|brisket|entrée|entree|sandwich|biscuits?)\b",
                line,
            ) and not re.search(
                r"(?i)\b(cup|tbsp|tsp|oz|lb|clove|tablespoon|teaspoon|stick|can)\b",
                line,
            ):
                continue
            # A count later in the line ("Giblets and neck from 1 turkey") is an
            # ingredient, not a dish title
            has_count = (
                bool(re.search(r"\d", line))
                or bool(re.search(r"(?i)\b(to taste|optional)\b", line))
            ) and len(line.split()) <= 12
            if not has_count and not re.search(
                r"(?i)\b(salt|pepper|flour|sugar|butter|oil|water|milk|egg|garlic|onion)\b",
                line,
            ):
                continue
            # "Dr Pepper..." has pepper but is a title — require short pantry line
            if not has_count and len(line.split()) > 6:
                continue
        ing = parse_ingredient_line(line)
        if not ing.name or is_junk_ingredient_line(ing.name) or is_non_ingredient_line(ing.name):
            continue
        # Reject if name is only a prep adjective
        if re.match(
            r"^(diced|minced|chopped|sliced|softened|small|large|medium)\b",
            ing.name,
            re.I,
        ) and len(ing.name.split()) <= 2:
            continue
        ingredients.append(ing)
    return ingredients


# Storage / serving tips inside the last instruction ("Remember to take them
# out 20 minutes before serving", "store them in the freezer") are notes, not
# cooking actions. Left as steps, each sentence became its own TRN column.
_NOTE_SENTENCE = re.compile(
    r"(?i)^(?:"
    r"remember\b|also,?\s+(?:don'?t|do not|be sure)\b|"
    r"(?:don'?t|do not)\s+stack\b|leftovers?\b|"
    r"(?:will|can|should)\s+keep\b|keeps?\s+(?:well|for|up to)\b|"
    r"to store\b|for storage\b|storage\b|make[- ]ahead\b|"
    r"this can be done in advance\b|"
    r"for a (?:more )?(?:moist(?:er)?|drier?|wetter|fluff(?:y|ier)|"
    r"crisp(?:y|ier)?|crunch(?:y|ier))\b"
    r")"
    r"|\bstored?\s+(?:them|it|these|leftovers|in)\b.*\b(?:freezer|fridge|"
    r"refrigerator|airtight|container|zip\s*-?lock|bag)\b"
)

# One-word imperatives ("Freeze.") are too short to survive is_junk_step on
# their own; fold them into the previous step instead of losing them.
_SHORT_IMPERATIVE = re.compile(
    r"(?i)^(?:freeze|chill|serve|refrigerate|cool|rest|cover|stir|bake|repeat)[.!]?$"
)


def _is_note_sentence(s: str) -> bool:
    return bool(_NOTE_SENTENCE.search((s or "").strip()))


def _split_step_lines(block: str, notes: Optional[list[str]] = None) -> list[str]:
    """
    Split instruction block into individual step strings.

    Important: never keep a multi-sentence blob as one step just because
    paragraphs were separated by a blank line (that produced whole recipes
    as a single Prep row when "grease"/"heat oven" appeared mid-text).
    """
    block = block.strip()
    if not block:
        return []

    # Normalize magazine end-numbers again (belt and suspenders)
    block = re.sub(r"([.!?])\s*(\d{1,2})\s+(?=[A-Z\"'])", r"\1\n\2. ", block)
    block = re.sub(r"([A-Za-z])\.(\d{1,2})\s+(?=[A-Z])", r"\1.\n\2. ", block)

    def finalize(parts: list[str]) -> list[str]:
        out = []
        for s in parts:
            # Further split any leftover multi-sentence chunks
            s = s.strip()
            if not s:
                continue
            subs = _split_sentences(s)
            for sub in subs:
                cleaned = _clean_step_text(sub)
                if not cleaned:
                    if out and _SHORT_IMPERATIVE.match(sub.strip()):
                        out[-1] = f"{out[-1]} {sub.strip()}"
                    continue
                # Notes only after a real step, so a recipe can't lose step 1
                if notes is not None and out and _is_note_sentence(cleaned):
                    notes.append(cleaned)
                    continue
                out.append(cleaned)
        return out

    # Numbered steps: 1. … 2. … or 1) …
    numbered = re.split(r"(?:^|\n)\s*\d+[\.\)]\s+", block)
    numbered = [s.strip() for s in numbered if s.strip()]
    if len(numbered) >= 2:
        return finalize(numbered)

    # Bullet list
    bullets = re.split(r"(?:^|\n)\s*[\-\*\u2022•]\s+", block)
    bullets = [s.strip() for s in bullets if s.strip()]
    if len(bullets) >= 2:
        return finalize(bullets)

    # One step per line when lines look like separate instructions
    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    action_lines = [
        ln
        for ln in lines
        if ACTION_START.match(ln)
        or HEADER_ACTIONS.match(ln)
        or re.match(r"(?i)^(combine|add|mix|fold|grease|roll|cut|arrange|heat|bake|remove|serve|cover|let|place|use)\b", ln)
    ]
    if len(action_lines) >= 2 and len(action_lines) >= len(lines) * 0.5:
        return finalize(lines)

    # Sentence split (always prefer this over giant paragraphs)
    sentences = _split_sentences(block)
    if len(sentences) >= 2:
        return finalize(sentences)

    cleaned = _clean_step_text(block)
    return [cleaned] if cleaned else []


def _split_sentences(text: str) -> list[str]:
    """Split on sentence boundaries; keep cooking steps intact."""
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", text)
    return [p.strip() for p in parts if p.strip()]


def _clean_step_text(s: str) -> str:
    """Remove trailing step numbers and drop non-instruction sentences."""
    s = scrub_inline(s or "").strip()
    s = re.sub(r"\s+\d{1,2}\s*$", "", s)  # trailing " 6"
    s = re.sub(r"\d{1,2}\.\s*$", "", s)  # trailing "6."
    s = re.sub(r"\s+", " ", s).strip()
    if not s or is_junk_step(s):
        return ""
    if re.match(r"^\d+\.?\s*$", s):
        return ""
    # Live-fire / campfire / "as in the book" variations — notes, not TRN columns
    if re.match(
        r"(?i)^("
        r"these (?:biscuits|cookies|can|may)|this (?:recipe )?can be made|"
        r"can be made over|as in the book|before beginning the recipe|"
        r"alternatively|variation:|optional method|note:|tip:|"
        r"proceed with the recipe as written|place a grate over"
        r")\b",
        s,
    ):
        return ""
    if re.search(
        r"(?i)\b(live fire|campfire|embers|wood or charcoal fire|"
        r"adjust the cook time to)\b",
        s,
    ):
        return ""
    # Section banners / cheer / nutrition leftovers as fake steps
    if re.match(r"(?i)^\[component\]", s):
        return ""
    if re.match(
        r"(?i)^("
        r"empalmes?|fried pinto|dried piquin|provecho|nutrition|"
        r"serving:|calories:|"
        r"about \d+ minutes?\.?$|some people love|go ahead and season|"
        r"set aside to proceed|do the same with"
        r")\b",
        s,
    ):
        return ""
    if re.search(r"(?i)\b(calories|carbohydrates|vitamin\s+[a-c]|kcal)\b", s):
        return ""
    # Pure tips / commentary (not the main action)
    if re.match(
        r"(?i)^("
        r"the biscuits will|don'?t (?:over|under)|dragging will|"
        r"it is important to|this will help|do not overmix|"
        r"when finished,|repeat as needed|note that|"
        r"about \d+ minutes|once the oil is hot,?"
        r")\b",
        s,
    ):
        # Keep "Once the oil is hot, stir in…" by stripping opener below in short_action
        if re.match(r"(?i)^once the oil is hot,?\s+\w+", s):
            pass
        elif re.match(r"(?i)^(about \d+ minutes|some people|do the same)\b", s):
            return ""
        elif re.match(
            r"(?i)^(the biscuits will|don'?t over|dragging will|it is important|"
            r"this will help|do not overmix|when finished|repeat as needed|note that)\b",
            s,
        ):
            return ""
    return s


def _short_action(text: str) -> str:
    """
    Derive a short CFE-style action label from the LEADING verb only.
    Delegates opener-stripping logic aligned with trn_engine._lead_action.
    """
    text = text.strip()
    text = re.sub(r"\b([A-Za-z]+)\s*-\s*([A-Za-z]+)\b", r"\1-\2", text)
    text = re.sub(
        r"^(?:lightly|gently|carefully|slightly|when finished,?|"
        r"once the oil is hot,?|once hot,?)\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^use\s+a\s+[\w\s\-½¼¾/.]+?\s+to\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^(?:in\s+a\s+\w+|in\s+the\s+\w+|using\s+a\s+\w+|with\s+a\s+\w+),?\s+",
        "",
        text,
        flags=re.I,
    )

    if re.search(r"\bovernight\b", text, re.I) and re.search(
        r"\b(rub|refrigerate|marinate|coat)\b", text, re.I
    ):
        if not re.search(r"\bsmoke\b", text, re.I):
            return "marinate"

    m = re.match(
        r"^\s*(?:meanwhile,?\s+|then,?\s+|next,?\s+|finally,?\s+)?"
        r"(?P<verb>"
        r"preheat|grease|butter\s+and\s+flour|line|spray|prepare|"
        r"melt|cream|whisk|beat|fold(?:\s+in)?|sift|mix(?:\s+in|\s+together)?|"
        r"combine|stir(?:\s+in|-?fry)?|toss|bake|roast|simmer|sear|brown|"
        r"fry|saute|sauté|grill|broil|boil|blend|knead|whip|pour|assemble|"
        r"marinate|refrigerate|chill|smoke|cook|heat|season|garnish|serve|"
        r"add|place|pat|rub|transfer|return|remove|reduce|bring|cover|drain|"
        r"skim|strain|spoon|coat|spread|top|roll|cut|arrange|flour|"
        r"sprinkle|drizzle|layer|slice|chop|dice|mince"
        r")\b",
        text,
        re.I,
    )
    if m:
        verb = re.sub(r"\s+", " ", m.group("verb").lower())
        canon = {
            "fold in": "fold in",
            "mix in": "mix",
            "mix together": "mix",
            "stir in": "stir in",
            "stir-fry": "stir-fry",
            "combine": "mix",
            "beat in": "beat",
            "whisk in": "whisk",
            "butter and flour": "butter and flour",
            "saute": "sauté",
            "refrigerate": "marinate",
        }
        return canon.get(verb, verb)

    if ACTION_START.match(text):
        return ACTION_START.match(text).group("verb").lower()[:20]  # type: ignore
    words = re.findall(r"[a-zA-Z\-]+", text)
    stop = {"the", "a", "an", "these", "this", "when", "use", "it"}
    for w in words:
        if w.lower() not in stop and len(w) > 2:
            return w.lower()[:20]
    return "step"


def _match_ingredients(step_text: str, ingredients: list[Ingredient]) -> list[str]:
    """
    Find which ingredients are mentioned in this step.
    Longer / multi-word names win first so "Dr Pepper" is not stolen by
    "red pepper flakes".
    """
    lower = step_text.lower()
    matched: list[str] = []
    used_spans: list[tuple[int, int]] = []

    adjectives = {
        "large", "small", "medium", "fresh", "ground", "dried", "chopped",
        "sliced", "minced", "optional", "cooked", "for", "serving", "purpose",
        "baking",  # "baking powder" must not claim "before baking"
        "with", "and", "or", "the", "from", "into", "onto", "a", "an", "of",
        "to", "in", "on", "at", "by", "as",
        "all", "unsalted", "salted", "white", "black", "red", "green", "whole",
        "boneless", "skinless", "thin", "grated", "crushed", "prepared",
        "four", "ounce", "ounces", "cans", "about", "long", "piece",
    }
    generic_heads = {
        "sauce", "extract", "powder", "flakes", "leaves", "cloves", "breast",
        "fillet", "fillets", "thighs", "pieces", "stock", "broth", "cans",
        "mix",  # "cornbread mix" must not claim "Mix in the onions"
    }
    short_ambiguous = {"pepper", "salt", "oil", "sugar", "butter", "sauce", "stock"}

    def candidates_for(ing: Ingredient) -> list[str]:
        name = re.sub(r"\([^)]*\)", "", ing.name.lower()).strip()
        # Bare "sage to taste" → "sage" (comma forms like "x, or to taste" unchanged)
        name = re.sub(r"^([a-z][a-z' ]*?)\s+to taste$", r"\1", name)
        name = re.sub(r"\s+", " ", name)
        if not name or len(name) < 2:
            return []
        # "1 egg beaten with a splash of milk, for egg wash" must match
        # "egg wash", not the dough eggs or a lone "milk"/"with".
        purpose = (ing.quantity.note or "").strip()
        pm = re.match(r"(?i)^for\s+(.+)$", purpose)
        if pm and pm.group(1).strip():
            return [pm.group(1).strip().lower()]
        tokens = re.findall(r"[a-zA-Z][a-zA-Z\-']+", name)
        cands = [name]
        if tokens:
            head = tokens[-1]
            if head not in generic_heads and head not in short_ambiguous:
                cands.append(head)
            if len(tokens) >= 2:
                cands.append(" ".join(tokens[-2:]))
                if tokens[0] in {"dr", "dr."} and len(tokens) >= 2:
                    cands.insert(0, " ".join(tokens[:2]))
                # "olive oil" / "vegetable oil" — step often says just "oil"
                if head in short_ambiguous:
                    cands.append(tokens[0])  # olive, vegetable, sesame…
                    cands.append(head)  # allow "oil" as last resort for this ing
            for tok in tokens:
                if (
                    len(tok) >= 4
                    and tok not in adjectives
                    and tok not in generic_heads
                    and tok not in short_ambiguous
                ):
                    cands.append(tok)
        seen = set()
        out = []
        for c in sorted(cands, key=len, reverse=True):
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    # Multi-word phrases for every ingredient first, then long single words,
    # then short ones — so "turkey broth" is claimed by the broth before the
    # lone word "turkey" from a longer ingredient name can steal it.
    def pass_of(cand: str) -> int:
        if " " in cand:
            return 0
        return 1 if len(cand) >= 6 else 2

    for current_pass in (0, 1, 2):
        ordered = sorted(ingredients, key=lambda i: len(i.name), reverse=True)
        for ing in ordered:
            if ing.id in matched:
                continue
            for cand in candidates_for(ing):
                if pass_of(cand) != current_pass:
                    continue
                if cand in adjectives or cand in generic_heads:
                    continue
                # bare "oil"/"salt"/"pepper" only if this ingredient's head noun
                if cand in short_ambiguous and " " not in cand:
                    # Ignore "(plus more to taste)" when finding the head noun
                    base = re.sub(r"\([^)]*\)", "", ing.name.lower())
                    base = re.sub(r"^\s*([a-z][a-z' ]*?)\s+to taste\s*$", r"\1", base)
                    tokens = re.findall(r"[a-zA-Z]+", base)
                    if not tokens or tokens[-1] != cand:
                        continue
                pattern = r"\b" + re.escape(cand) + r"s?\b"
                for m in re.finditer(pattern, lower):
                    span = m.span()
                    if any(not (span[1] <= a or span[0] >= b) for a, b in used_spans):
                        continue
                    matched.append(ing.id)
                    used_spans.append(span)
                    break
                if ing.id in matched:
                    break

    collectives = {
        r"\baromatics?\b": re.compile(
            r"(?i)\b(garlic|ginger|onion|shallot|scallion|green onion|leek|celery)\b"
        ),
        r"\bspices?\b": re.compile(
            r"(?i)\b(salt|coriander|cumin|paprika|chili|cayenne|"
            r"cinnamon|nutmeg|oregano|thyme|rosemary|seasoning|"
            r"red pepper|black pepper|ground pepper)\b"
        ),
        r"\bdry ingredients?\b": re.compile(
            r"(?i)\b(flour|masa|sugar|baking powder|baking soda|salt|cocoa|starch)\b"
        ),
        r"\bwet ingredients?\b": re.compile(
            r"(?i)\b(milk|buttermilk|cream|water|broth|stock|oil|butter|egg)\b"
        ),
    }
    for pat, name_re in collectives.items():
        if re.search(pat, lower):
            for ing in ingredients:
                if re.search(r"(?i)\b(dr\.?\s*pepper|pepsi|coke|cola|soda)\b", ing.name):
                    continue
                if name_re.search(ing.name) and ing.id not in matched:
                    matched.append(ing.id)

    id_order = {ing.id: i for i, ing in enumerate(ingredients)}
    matched = sorted(set(matched), key=lambda i: id_order.get(i, 999))
    return matched



def _parse_steps(
    block: str, ingredients: list[Ingredient], notes: Optional[list[str]] = None
) -> list[Step]:
    raw_steps = _split_step_lines(block, notes)
    steps: list[Step] = []
    body_indices: list[int] = []  # indices in `steps` that are body steps

    for raw in raw_steps:
        # Collapse internal newlines for matching
        text = re.sub(r"\s+", " ", raw).strip()
        if not text or len(text) < 3:
            continue
        if ING_HEADERS.match(text) or STEP_HEADERS.match(text):
            continue

        # Prep ONLY if the step *starts* as prep (preheat / grease pan).
        # Never mark a long multi-action blob as prep because "grease" appears mid-text.
        # Strip "When ready to bake …, preheat …" so the true prep verb is first.
        header_text = re.sub(
            r"(?i)^when ready to \w+(?:\s+\w+){0,4},?\s+",
            "",
            text,
        )
        is_header = bool(HEADER_ACTIONS.match(header_text)) or bool(
            re.match(
                r"(?i)^(preheat|heat\s+(?:your\s+|the\s+)?(?:home\s+)?oven|"
                r"grease|butter and flour|line (?:the )?(?:pan|sheet))\b",
                header_text,
            )
        )
        # Short pure prep only — long steps that also mix/bake are body steps
        if is_header and (
            len(header_text) > 120
            or re.search(
                r"(?i)\b(combine|mix|add|fold|bake|stir|pour|cook|smoke|rub)\b",
                header_text,
            )
        ):
            # "Heat oven to 425" alone → prep; "Heat oven… then bake…" → body
            if not re.match(
                r"(?i)^(preheat|heat\s+(?:your\s+|the\s+)?(?:home\s+)?oven|"
                r"grease|butter and flour|line ).{0,80}$",
                header_text,
            ):
                is_header = False

        ids = _match_ingredients(text, ingredients)

        action = _short_action(text)
        duration = ""
        # Prefer ranges: "30 to 40 minutes", "25–30 min"
        dm = re.search(
            r"(\d+\s*(?:to|–|-|—)\s*\d+\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?)"
            r"|\d+\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?))",
            text,
            re.I,
        )
        if dm:
            duration = dm.group(1)

        step = Step(
            action=action,
            detail=text,
            ingredient_ids=ids,
            is_header=is_header,
            duration=duration,
        )
        steps.append(step)
        if not is_header:
            body_indices.append(len(steps) - 1)

    # Wire consumes_steps so the TRN cascade can grow rowspans correctly.
    # Every non-header step after the first inherits the running mixture
    # unless it looks like a pure parallel prep of only brand-new ingredients.
    seen_ings: set[str] = set()
    for i, step in enumerate(steps):
        if step.is_header:
            continue
        priors = [j for j in range(i) if not steps[j].is_header]
        if not priors:
            seen_ings.update(step.ingredient_ids)
            continue

        new_only = set(step.ingredient_ids) - seen_ings
        mentions_mixture = bool(
            COMBINE_PHRASES.search(step.detail)
            or re.search(
                r"\b(batter|mixture|dough|sauce|fold|pour|bake|return|"
                r"combine|together|whisk in|stir in|beat in|mix in)\b",
                step.detail,
                re.I,
            )
        )
        no_direct = not step.ingredient_ids
        # Parallel if this step only names brand-new ings and does not
        # combine/fold into something already going
        parallel = (
            bool(new_only)
            and set(step.ingredient_ids) == new_only
            and not mentions_mixture
            and not no_direct
        )
        if not parallel:
            # Consume the immediately previous body step (cascade spine)
            step.consumes_steps = [priors[-1]]
        seen_ings.update(step.ingredient_ids)

    return steps


def _infer_ingredients_from_steps(block: str) -> list[Ingredient]:
    """Last-resort: pull quoted-looking quantity phrases from steps."""
    found: list[Ingredient] = []
    seen = set()
    # "2 cups flour", "1 tsp salt"
    for m in re.finditer(
        r"(\d[\d\s/½¼¾⅓⅔.]*\s*"
        r"(?:cups?|tbsp|tsp|tablespoons?|teaspoons?|oz|lb|g|ml|cloves?)\s+"
        r"[a-zA-Z][a-zA-Z\s\-]{2,30})",
        block,
        re.I,
    ):
        line = m.group(1).strip()
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(parse_ingredient_line(line))
    return found


def _is_junk_step(step: Step) -> bool:
    """Filter scraper leftovers that must never become TRN columns."""
    t = f"{step.action} {step.detail}".lower()
    if re.search(
        r"\b("
        r"recipe\s+notes?|storage\s*:|refrigerate\s+leftovers|"
        r"reviews?\s+filed|filed\s+in\s*:|make\s+ahead|budget\s+chicken|"
        r"print\s+recipe|jump\s+to\s+recipe|rate\s+this|leave\s+a\s+comment|"
        r"nutrition\s+facts|calories\s*:"
        r")\b",
        t,
    ):
        return True
    # Pure "Reviews …" lines
    if re.match(r"^\s*reviews?\b", step.detail or "", re.I):
        return True
    return False


# Max action columns in a CFE-style card. Real tables are ~4–7 wide, never 15.
MAX_TRN_ACTION_COLS = 6

# Verbs that are logistics / plating — not worth their own column
_LOGISTICS = re.compile(
    r"\b("
    r"transfer|set\s+aside|reduce\s+(?:the\s+)?heat|remove\s+from|"
    r"return\s+to|place\s+on|move\s+to|reserve|discard|"
    r"meanwhile|repeat|set\s+the\s+aside"
    r")\b",
    re.I,
)

_MAJOR = re.compile(
    r"\b("
    r"melt|cream|whisk|beat|fold|sift|bake|roast|simmer|sear|brown|"
    r"stir-?fry|saute|sauté|grill|broil|boil|blend|knead|fry|"
    r"mix|combine|toss|marinate|chill|whip|pour|assemble|garnish|serve"
    r")\b",
    re.I,
)


def compress_steps_for_table(
    steps: list[Step],
    max_cols: int = MAX_TRN_ACTION_COLS,
) -> list[Step]:
    """
    Deprecated path — display compression lives in trn_engine.build_trn().

    Kept as a thin adapter for any old callers: returns header steps +
    body steps unchanged (engine does the real work at render time).
    """
    from copy import deepcopy

    return [deepcopy(s) for s in steps if not _is_junk_step(s)]


def apply_scale(table: TRNTable, factor: float) -> TRNTable:
    """
    Return a copy of the table with scale_factor set.
    Quantities are displayed via Ingredient.display_left(factor);
    we also lightly rewrite step detail numbers.
    """
    from copy import deepcopy
    from .quantity_parser import scale_text_amounts

    t = deepcopy(table)
    old = t.scale_factor or 1.0
    # Relative to original (unscaled) amounts stored on ingredients
    # Ingredients store base amounts; scale_factor is display multiplier.
    t.scale_factor = factor
    # Update step text from base (divide out old scale then apply new)
    relative = factor / old if old else factor
    if relative != 1.0:
        for step in t.steps:
            step.detail = scale_text_amounts(step.detail, relative)
            if step.duration:
                # Don't scale times
                pass
    return t
