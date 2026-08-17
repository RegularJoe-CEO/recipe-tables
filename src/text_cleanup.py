"""
Normalize messy recipe text from PDFs, OCR, and magazine scrapes.

Texas Monthly / magazine PDFs often produce:
  • Ligatures: ﬂour → need "flour"
  • Glued headers: "Directions 2 cups flour"
  • End-of-line step numbers: "salt. 2" / "degrees F.1 In a bowl"
  • Wrapped ingredient lines: "butter,\\ndiced small"
  • Brand notes as fake ingredients: "(some brands include …)"
  • Intro blurbs before ingredients

This module runs before convert_text_to_trn so the engine sees clean copy.
"""

from __future__ import annotations

import re
from typing import List, Tuple


def cleanup_recipe_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _fix_ligatures(text)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    # Drop image URL debris from scrapers (incl. broken "3.3.1&q=45&w=350)")
    text = re.sub(r"\S+\.(?:jpg|jpeg|png|gif|webp)\?[^\s)]+", "", text, flags=re.I)
    text = re.sub(r"\S+\.(?:jpg|jpeg|png|gif|webp)\b", "", text, flags=re.I)
    text = re.sub(r"(?m)^\s*[\d.]{1,8}&q=\d[^\n]*$", "", text)
    text = re.sub(r"(?m)^\s*[\d.]+&[a-z]=\d[^\n]*\)?\s*$", "", text)
    text = _fix_glued_units(text)
    text = _strip_nutrition_blocks(text)
    text = strip_equipment_section(text)
    text = _unglue_section_headers(text)
    text = _normalize_component_headers(text)
    text = _normalize_step_numbers(text)
    text = _join_wrapped_ingredient_continuations(text)
    text = _merge_brand_note_continuations(text)
    text = _drop_intro_blurbs(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fix_glued_units(text: str) -> str:
    """
    PDF extract often glues: '2tablespoonsof oil' → '2 tablespoons of oil'
    '1/2cuppinto' → '1/2 cup pinto', '4corn tortillas' → '4 corn tortillas'
    """
    units = (
        r"tablespoons?|teaspoons?|tbsp|tsp|cups?|ounces?|oz|pounds?|lbs?|"
        r"grams?|ml|cloves?|kernels?|cans?|slices?|sticks?"
    )
    # Full glue: 2tablespoonsof / 1/2cupof / 1teaspoonof
    text = re.sub(
        rf"(?i)(\d+\s*/\s*\d+|\d+)({units})(of)?(?=[a-zA-Z]|\s|$)",
        lambda m: f"{m.group(1)} {m.group(2)}"
        + (f" {m.group(3)}" if m.group(3) else ""),
        text,
    )
    # unit glued to next food word without "of": cuppinto, tablespoonspiquin
    text = re.sub(
        rf"(?i)\b({units})(?=[a-z]{{3,}})",
        r"\1 ",
        text,
    )
    # number glued to food: "4corn" "2eggs" "2chorizos"
    text = re.sub(
        r"(?i)(\d+)(corn|eggs?|tortillas?|chorizos?|onions?|garlic|beans?)\b",
        r"\1 \2",
        text,
    )
    # "Servings2" → "Servings 2"
    text = re.sub(r"(?i)\b(servings?|yield|makes?|calories)(\d)", r"\1 \2", text)
    # "1Empalme" mid nutrition
    text = re.sub(r"(\d)([A-Z][a-z])", r"\1 \2", text)
    # collapse double spaces
    text = re.sub(r" {2,}", " ", text)
    return text


def _strip_nutrition_blocks(text: str) -> str:
    """
    Remove nutrition widgets but keep any ingredient lines glued after them.
    e.g. '...Iron: 2.9mg1/3 of a small white onion' → keep the onion line.
    """
    salvaged: list[str] = []

    def repl(m: re.Match) -> str:
        block = m.group(0)
        # Pull qty-led fragments out of the nutrition sludge
        for frag in re.findall(
            r"(?i)(?:mg|iu|kcal)\s*([\d½¼¾⅓⅔/\.]+\s*(?:of\s+)?[a-z][a-z\s,/]*?)(?=\d+\s*(?:mg|iu|kcal)|$)",
            block,
        ):
            frag = frag.strip(" |")
            if re.match(r"^[\d½¼¾⅓⅔/]", frag) and len(frag) > 4:
                salvaged.append(frag)
        # Simpler: after last mg/IU, rest of string
        found = re.search(r"(?i)(?:mg|iu|kcal)\s*([\d½¼¾⅓⅔/].+)$", block)
        if found:
            rest = found.group(1).strip()
            # May contain multiple ings separated oddly — take leading qty phrase
            if re.match(r"^[\d½¼¾⅓⅔/]", rest):
                salvaged.append(rest.split("|")[0].strip())
        return "\n"

    text = re.sub(
        r"(?is)\bnutrition\b.*?(?=\n\s*\d+[\.\)]\s|\n\s*directions?\b|\n\s*instructions?\b|\Z)",
        repl,
        text,
        count=1,
    )
    # Inline "Serving: 1 Empalme | Calories: … Iron: 2.9mg1/3 of onion"
    def inline_repl(m: re.Match) -> str:
        tail = m.group(0)
        found = re.search(r"(?i)(?:mg|iu|kcal)\s*([\d½¼¾⅓⅔/].*)$", tail)
        if found:
            salvaged.append(found.group(1).strip().split("\n")[0])
            return "\n" + found.group(1).strip() + "\n"
        return "\n"

    text = re.sub(
        r"(?im)^.*\b(serving:\s*\d|calories\s*:|carbohydrates\s*:).*$",
        inline_repl,
        text,
    )
    # Standalone leftover nutrition lines (no salvaged qty)
    text = re.sub(
        r"(?im)^.*\b(cholesterol|sodium|potassium|vitamin\s+[a-z]|saturated\s+fat)\b.*$",
        "",
        text,
    )
    if salvaged:
        # Prefer inserting before Instructions if present
        block = "\n".join(dict.fromkeys(salvaged))  # dedupe
        if re.search(r"(?im)^instructions?\b", text):
            text = re.sub(
                r"(?im)^(instructions?\b)",
                block + "\n\n\\1",
                text,
                count=1,
            )
        else:
            text = text + "\n" + block
    return text


def _normalize_component_headers(text: str) -> str:
    """
    Multi-component recipes (Mexico in My Kitchen style):
      EMPALMES / FRIED PINTO BEANS / DRIED PIQUIN PEPPER SALSA
    Keep as section markers, not ingredients. Drop MAKES/TO SERVE chrome.
    """
    lines = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        # Drop pure chrome; salvage qty glued after "MAKES ABOUT ½ A CUP*4corn tortillas"
        if re.match(
            r"^(?i)(makes?\s+\d|makes?\s+about|to serve:?|course\b|cuisine\b|"
            r"keyword\b|author\b|prep time|cook time|total time|provecho!?|"
            r"calories\b)",
            s,
        ):
            salvaged = re.findall(
                r"(?i)(\d+\s+(?:corn\s+)?tortillas?|\d+\s+tablespoons?\s+of\s+\w+|"
                r"\d+\s*/\s*\d+\s+cups?\s+\w+)",
                s,
            )
            for frag in salvaged:
                lines.append(frag.strip())
            # also *4corn tortillas style
            m = re.search(r"(?i)\*?\s*(\d+\s*corn\s+tortillas?)", s)
            if m:
                lines.append(re.sub(r"(?i)(\d+)(corn)", r"\1 \2", m.group(1)))
            continue
        # ALL-CAPS component titles → mark as comments for later filter
        if (
            len(s) > 3
            and s == s.upper()
            and re.search(r"[A-Z]{3,}", s)
            and not re.match(r"^[\d½¼¾]", s)
            and not re.search(r"\d", s)  # not "4 CUPS"
        ):
            # Keep as a light section note the step filter will drop
            if re.search(
                r"(?i)(empalme|bean|salsa|sauce|tortilla|chorizo|soup|rice|meat|salad)",
                s,
            ):
                lines.append(f"[component] {s.title()}")
                continue
            continue
        lines.append(s)
    return "\n".join(lines)


def _fix_ligatures(text: str) -> str:
    # Common PDF ligatures that break "flour", "filet", etc.
    repl = {
        "\ufb00": "ff",  # ﬀ
        "\ufb01": "fi",  # ﬁ
        "\ufb02": "fl",  # ﬂ
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "ﬂ": "fl",
        "ﬁ": "fi",
        "ﬀ": "ff",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
        "\u2019": "'",
        "\u2018": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "½": "1/2",
        "¼": "1/4",
        "¾": "3/4",
        "⅓": "1/3",
        "⅔": "2/3",
        "⅛": "1/8",
        "⅜": "3/8",
        "⅝": "5/8",
        "⅞": "7/8",
        "1½": "1 1/2",
        "1¼": "1 1/4",
    }
    for a, b in repl.items():
        text = text.replace(a, b)
    # Also "1¼" style already handled; bare "ﬂour"
    text = text.replace("ﬂour", "flour").replace("ﬁlet", "filet")
    # "1" + "1/4" from "1¼" → "11/4" — insert space between digit and fraction
    text = re.sub(
        r"(\d)(1/2|1/4|3/4|1/3|2/3|1/8|3/8|5/8|7/8)\b",
        r"\1 \2",
        text,
    )
    return text


def _unglue_section_headers(text: str) -> str:
    """
    'Directions 2 cups flour' → 'Directions\\n2 cups flour'
    'Ingredients 1 cup sugar' → split
    """
    text = re.sub(
        r"(?im)^(ingredients?|directions?|instructions?|method|preparation|steps?)\s+"
        r"(?=\d|[\d½¼¾⅓⅔⅛]|a\s|an\s|the\s|[A-Z])",
        lambda m: m.group(1).title() + "\n",
        text,
    )
    # Standalone SERVINGS / YIELD labels → normalize
    text = re.sub(r"(?im)^servings?\s*$", "Servings:", text)
    text = re.sub(r"(?im)^yield\s*$", "Yield:", text)
    # "12 biscuits" after Servings: keep as servings line
    text = re.sub(
        r"(?im)^(servings?|yield)\s*:?\s*\n\s*(\d+\s*(?:biscuits?|cookies?|servings?|people|portions?)?)\s*$",
        r"Servings: \2",
        text,
    )
    return text


def _normalize_step_numbers(text: str) -> str:
    """
    Magazine style puts step numbers at the END of each sentence:
      'Heat oven to 425 F.1\\nIn a bowl...salt. 2\\nAdd buttermilk.3'
    Convert to:
      '1. Heat oven...\\n2. In a bowl...\\n3. Add buttermilk...'
    Also: 'degrees F.1 In a medium' mid-line.
    """
    # Mid-line: "F.1 In" or "salt. 2 Add" or "combine.3 Fold"
    text = re.sub(
        r"([.!?])\s*(\d{1,2})\s+(?=[A-Z\"'])",
        r"\1\n\2. ",
        text,
    )
    # End of line only: "pan.5" or "pan. 5"
    text = re.sub(r"([.!?])\s*(\d{1,2})\s*$", r"\1\n\2.", text, flags=re.M)
    # "F.1" glued without space before next capital on same line — already handled

    # Renumber mess: lines that are just "2." or "3." after split — fine
    # Convert trailing " 6" on "Serve warm. 6" 
    text = re.sub(r"([.!?])\s+(\d{1,2})\s*$", r"\1\n\2.", text, flags=re.M)

    # Rebuild proper "N. sentence" order when we have "sentence\nN."
    # Actually after above we get "sentence.\n2. next" which is good IF we
    # move numbers to the START of the FOLLOWING sentence.
    # Pattern produced: "Heat...F.\n1. \nIn a medium" — number before blank
    # Fix: ".\n1. \nIn" → ".\n1. In"
    text = re.sub(r"(\d{1,2})\.\s*\n+(?=[A-Za-z])", r"\1. ", text)

    # When number ended the PREVIOUS sentence, associate with NEXT:
    # We already insert "\n2. " before next capital — good.

    # "Heat...F.1\nIn" might still be "F.1\nIn" if no space: handle
    text = re.sub(r"([A-Za-z])\.(\d{1,2})\s*\n", r"\1.\n\2. ", text)
    text = re.sub(r"([A-Za-z])\.(\d{1,2})\s+(?=[A-Z])", r"\1.\n\2. ", text)

    return text


def _join_wrapped_ingredient_continuations(text: str) -> str:
    """
    Join:
      '12 tablespoons butter,\\ndiced small' → '12 tablespoons butter, diced small'
      '1 cup masa harina\\n(some brands...)'  → keep brand note on same or next handled separately
    """
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            # Continuation: starts with lowercase prep note, no leading qty
            if (
                line
                and nxt
                and not re.match(r"^(ingredients?|directions?|instructions?|method)\b", line, re.I)
                and not re.match(r"^(ingredients?|directions?|instructions?|method)\b", nxt, re.I)
                and not re.match(r"^[\d½¼¾⅓⅔]", nxt)
                and not re.match(r"^\d+[\.\)]\s", nxt)
                and (
                    re.match(
                        r"^(diced|minced|chopped|sliced|softened|melted|cubed|divided|"
                        r"room\s+temperature|cold|soft|large|small|medium|plus|or)\b",
                        nxt,
                        re.I,
                    )
                    or (line.rstrip().endswith(",") and nxt[0].islower())
                )
            ):
                out.append(line.rstrip(", ") + ", " + nxt)
                i += 2
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _merge_brand_note_continuations(text: str) -> str:
    """
    Merge:
      '1 cup masa harina'
      '(some brands include Maseca,'
      'Mill)'
    into one ingredient line (or drop the brand parenthetical entirely —
    brand shopping notes are not TRN ingredients).
    """
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        # Drop pure brand-note lines / continuations
        if _is_brand_or_shopping_note(line):
            i += 1
            continue
        # If line ends mid-paren brand note, skip following until paren closes
        if re.search(r"\(some brands|\(or any|\(such as|\(e\.g\.|\(eg\b", line, re.I):
            # strip from brand note onward
            line = re.sub(
                r"\s*\((?:some brands|or any|such as|e\.g\.|eg\b).*$",
                "",
                line,
                flags=re.I,
            ).strip()
            i += 1
            while i < len(lines) and (
                _is_brand_or_shopping_note(lines[i])
                or re.match(r"^[A-Za-z].*\)$", lines[i].strip())
                or "(" not in lines[i]
                and ")" in lines[i]
            ):
                i += 1
            if line:
                line = re.sub(r"\s*\(\s*$", "", line).strip()
                out.append(line)
            continue
        # orphan open paren leftovers from stripped brand notes
        line = re.sub(r"\s*\(\s*$", "", line).strip()
        line = re.sub(r",\s*\(\s*diced", ", diced", line, flags=re.I)
        out.append(line)
        i += 1
    return "\n".join(out)


def _is_brand_or_shopping_note(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.match(
        r"^\((?:some brands|or any|such as|e\.g\.|eg\b|available|store-bought)",
        s,
        re.I,
    ):
        return True
    if re.match(r"^(maseca|masienda|king arthur|bob'?s red|gold medal)\b", s, re.I):
        return True
    if re.match(r"^[A-Za-z][A-Za-z\s,'&]+Mill\)?\s*$", s) and "cup" not in s.lower():
        # "Mill)" leftover from Bob's Red Mill
        if re.search(r"mill\)?$", s, re.I):
            return True
    if s in {")", "Mill)", "Mill"}:
        return True
    return False


def strip_equipment_section(text: str) -> str:
    """Remove Equipment / Tools blocks so gear is never food."""
    return re.sub(
        r"(?is)(?:^|\n)(?:equipment|tools?|special\s+equipment)\s*\n.*?"
        r"(?=\n(?:ingredients?|directions?|instructions?|method)\b|\Z)",
        "\n",
        text,
    )


def _drop_intro_blurbs(text: str) -> str:
    """
    Drop marketing intro between title and Ingredients:
      'Use these as bases for killer breakfast sandwiches...'
    Keep title line and section headers.
    """
    lines = text.split("\n")
    if not lines:
        return text
    # Find ingredients / directions header index
    header_i = None
    for i, ln in enumerate(lines):
        if re.match(r"^(ingredients?|directions?|instructions?|method|steps?)\b", ln.strip(), re.I):
            header_i = i
            break
    if header_i is None or header_i <= 1:
        return text

    title = lines[0]
    kept = [title]
    # Keep servings/yield lines before ingredients
    for ln in lines[1:header_i]:
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^(servings?|yield|serves?|makes?)\b", s, re.I):
            kept.append(s)
            continue
        if re.match(r"^\d+\s+(biscuits?|cookies?|servings?|people)\b", s, re.I):
            kept.append("Servings: " + s)
            continue
        # Drop prose blurb (no quantity, long, starts with Use/This/These/Perfect)
        if re.match(r"^(use these|this |these |perfect |great |try |make these)\b", s, re.I):
            continue
        if len(s) > 60 and not re.match(r"^[\d½¼¾]", s):
            continue
        # short non-food lines drop
        if not re.match(r"^[\d½¼¾]", s) and not re.match(
            r"^(servings?|yield)", s, re.I
        ):
            # might still be part of blurb
            if re.search(r"\b(sandwich|side dish|weeknight|gathering|cleanup)\b", s, re.I):
                continue
        kept.append(s)
    kept.extend(lines[header_i:])
    return "\n".join(kept)


def is_non_ingredient_line(line: str) -> bool:
    """True if line should never become an ingredient row."""
    s = line.strip()
    if not s:
        return True
    if re.match(r"^\[component\]", s, re.I):
        return True
    # ALL-CAPS section titles
    if len(s) > 4 and s == s.upper() and re.search(r"[A-Z]{4,}", s) and not re.match(
        r"^[\d½¼¾]", s
    ):
        return True
    if re.match(
        r"^(ingredients?|directions?|instructions?|method|steps?|preparation|"
        r"equipment|tools?|special\s+equipment|to serve|makes?\b|"
        r"fried\s+pinto|dried\s+piquin|empalmes?\b)\b",
        s,
        re.I,
    ):
        return True
    if _is_brand_or_shopping_note(s):
        return True
    # Kitchen equipment is not food
    if re.match(
        r"^\d+\s+(smoker|grill|oven|skillet|dutch\s+oven|sheet\s+pan|baking\s+sheet|"
        r"roasting\s+pan|thermometer|mixer|blender|processor|wok|pot|pan)\b",
        s,
        re.I,
    ):
        return True
    if re.match(
        r"^(smoker|grill|aluminum\s+pan|roasting\s+pan|meat\s+thermometer)\b",
        s,
        re.I,
    ):
        return True
    # Prep-only fragment without quantity or food name
    if re.match(
        r"^(diced|minced|chopped|sliced|softened|melted|cubed|divided|small|large|medium)\s*(small|large|medium)?\s*$",
        s,
        re.I,
    ):
        return True
    # Pure commentary
    if re.match(r"^(note|tip|variation|optional)\s*:", s, re.I):
        return True
    return False
