"""
Parse free-text ingredient lines into structured Quantity + name.

Handles common patterns:
  "2 cups all-purpose flour"
  "1/2 tsp salt"
  "1½ tablespoons unsalted butter, softened"
  "salt to taste"
  "2-3 cloves garlic, minced"
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .models import Quantity, Ingredient

# Unicode / ascii fraction glyphs → float
FRAC_MAP = {
    "½": 0.5,
    "¼": 0.25,
    "¾": 0.75,
    "⅓": 1 / 3,
    "⅔": 2 / 3,
    "⅛": 0.125,
    "⅜": 0.375,
    "⅝": 0.625,
    "⅞": 0.875,
    "⅕": 0.2,
    "⅖": 0.4,
    "⅗": 0.6,
    "⅘": 0.8,
}

UNIT_PATTERN = re.compile(
    r"""
    (?P<unit>
        (?:
            cups?|c\.?
            |tablespoons?|tbsps?|tbs\.?|tbsp\.?
            |teaspoons?|tsps?|tsp\.?
            |pounds?|lbs?|lb\.?
            |ounces?|ozs?|oz\.?
            |grams?|g\.?
            |kilograms?|kgs?|kg\.?
            |milliliters?|mls?|ml\.?
            |liters?|litres?|l\.?
            |fluid\s+ounces?|fl\.?\s*oz\.?
            |pints?|pts?|pt\.?
            |quarts?|qts?|qt\.?
            |gallons?|gals?|gal\.?
            |cloves?
            |slices?
            |pieces?
            |cans?
            |packages?|pkgs?|pkg\.?
            |bunches?
            |heads?
            |sticks?
            |pinches?
            |dashes?
            |handfuls?
            |sprigs?
            |leaves?
            |large|medium|small
            |whole
        )
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Leading quantity: 2, 1/2, 1 1/2, 1½, 1¼ (no space), 2-3, .5
QTY_RE = re.compile(
    r"""
    ^\s*
    (?P<qty>
        (?:
            \d+\s+[½¼¾⅓⅔⅛⅜⅝⅞]     # 1 ½  (digit + space + glyph)
            |\d+[½¼¾⅓⅔⅛⅜⅝⅞]         # 1½   (digit glued to glyph)
            |[½¼¾⅓⅔⅛⅜⅝⅞]            # ½ alone
            |\d+\s+\d+/\d+            # 1 1/2
            |\d+/\d+                  # 1/2
            |\d+\s*-\s*\d+(?:\.\d+)?  # 2-3
            |\d+\s+to\s+\d+(?:\.\d+)? # 2 to 4
            |\d+\.\d+                 # 1.5
            |\d+                      # 2
        )
    )
    \s*
    """,
    re.VERBOSE,
)

# Parenthetical notes often after the name: butter, softened → note=softened
NOTE_SPLIT = re.compile(r",\s*(.+)$")


def _parse_number(token: str) -> Optional[float]:
    token = token.strip()
    if not token:
        return None
    # Range → take midpoint for scaling, keep raw elsewhere
    if re.search(r"\s+to\s+", token, re.I):
        parts = re.split(r"\s+to\s+", token, maxsplit=1, flags=re.I)
        try:
            a = _parse_number(parts[0])
            b = _parse_number(parts[1])
            if a is not None and b is not None:
                return (a + b) / 2.0
        except Exception:
            pass
    if "-" in token and not token.startswith("-"):
        parts = [p.strip() for p in token.split("-", 1)]
        try:
            a = _parse_number(parts[0])
            b = _parse_number(parts[1])
            if a is not None and b is not None:
                return (a + b) / 2.0
        except Exception:
            pass
    # Mixed unicode: 1½ or "1 ½"
    for glyph, val in FRAC_MAP.items():
        if glyph in token:
            left = token.replace(glyph, "").strip()
            if left == "":
                return val
            try:
                return float(left) + val
            except ValueError:
                return val
    # Mixed ascii: 1 1/2
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)$", token)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    # Simple fraction 1/2
    m = re.match(r"^(\d+)/(\d+)$", token)
    if m:
        return int(m.group(1)) / int(m.group(2))
    try:
        return float(token)
    except ValueError:
        return None


def parse_ingredient_line(line: str) -> Ingredient:
    """
    Convert a single free-text ingredient line into an Ingredient.
    Falls back gracefully when quantity cannot be parsed.
    """
    original = line.strip()
    # Strip list markers
    original = re.sub(r"^[\-\*\u2022•]\s*", "", original)
    original = re.sub(r"^\d+[\.\)]\s*", "", original).strip()
    if not original:
        return Ingredient(name="", quantity=Quantity(amount=None, raw=""))

    amount: Optional[float] = None
    unit = ""
    rest = original
    raw_qty = ""

    m = QTY_RE.match(original)
    if m:
        raw_qty = m.group("qty")
        amount = _parse_number(raw_qty)
        rest = original[m.end() :].strip()

    # Unit at start of remainder
    um = UNIT_PATTERN.match(rest)
    if um and amount is not None:
        unit = um.group("unit").strip()
        # Normalize unit spelling a bit
        unit = _normalize_unit(unit)
        rest = rest[um.end() :].strip()
        # Drop leading "of "
        rest = re.sub(r"^of\s+", "", rest, flags=re.IGNORECASE)

    name = rest
    note = ""
    nm = NOTE_SPLIT.search(name)
    # Only peel note if it looks like a prep note, not part of the name.
    # "cooked, crumbled cornbread" / "drained, crushed pineapple" — the
    # comma separates adjectives, and the left token is not the food.
    _PREP_ONLY_NAMES = {
        "cooked", "drained", "diced", "minced", "chopped", "sliced",
        "softened", "melted", "crumbled", "toasted", "dry", "fresh",
        "frozen", "beaten", "peeled", "seeded", "grated", "shredded",
        "crushed", "unsalted", "salted", "ground", "dried", "smoked",
        "canned", "packed", "sifted", "large", "medium", "small", "whole",
    }
    if nm:
        candidate = nm.group(1).strip()
        # Heuristic: short prep phrases
        if len(candidate) < 40 and not re.search(
            r"\b(and|or|with)\b", candidate, re.I
        ):
            left = name[: nm.start()].strip()
            if left.lower() not in _PREP_ONLY_NAMES:
                name = left
                note = candidate

    name = name.strip(" ,")
    if not name:
        name = original

    qty = Quantity(
        amount=amount,
        unit=unit,
        raw=raw_qty if amount is not None else (original if amount is None and not unit else raw_qty),
        note=note,
    )
    # For unmeasured lines like "salt to taste"
    if amount is None and not unit:
        qty.raw = ""

    return Ingredient(name=name, quantity=qty)


def _normalize_unit(unit: str) -> str:
    u = unit.lower().rstrip(".")
    mapping = {
        "c": "cup",
        "cup": "cup",
        "cups": "cups",
        "tbsp": "tbsp",
        "tbs": "tbsp",
        "tablespoon": "tbsp",
        "tablespoons": "tbsp",
        "tsp": "tsp",
        "teaspoon": "tsp",
        "teaspoons": "tsp",
        "lb": "lb",
        "lbs": "lb",
        "pound": "lb",
        "pounds": "lb",
        "oz": "oz",
        "ounce": "oz",
        "ounces": "oz",
        "g": "g",
        "gram": "g",
        "grams": "g",
        "kg": "kg",
        "ml": "ml",
        "l": "L",
        "liter": "L",
        "litre": "L",
        "clove": "clove",
        "cloves": "cloves",
    }
    return mapping.get(u, unit)


def scale_text_amounts(text: str, factor: float) -> str:
    """
    Best-effort scale of numeric amounts embedded in free text
    (used for step detail updates). Leaves non-quantity numbers alone
    when they look like temperatures/times (followed by °F/°C/min/hour).
    """
    if factor == 1.0:
        return text

    def repl(m: re.Match) -> str:
        after = text[m.end() : m.end() + 12]
        if re.match(r"\s*(°|degrees?|F\b|C\b|min|hour|hr|sec|%)", after, re.I):
            return m.group(0)
        n = _parse_number(m.group(0))
        if n is None:
            return m.group(0)
        from .models import _format_amount

        return _format_amount(n * factor)

    return re.sub(
        r"\d+\s+[½¼¾⅓⅔⅛⅜⅝⅞]|\d+\s+\d+/\d+|\d+/\d+|[½¼¾⅓⅔⅛⅜⅝⅞]|\d+\.\d+|\d+",
        repl,
        text,
    )
