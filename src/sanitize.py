"""
Aggressive content sanitizer for Recipe Tables.

Scraped recipe pages are full of ad crap, affiliate junk, streaming promos,
nutrition widgets, and "Visit foo.com" CTAs. None of that belongs in a TRN
card or PDF.

Rules (applied everywhere: import → convert → export):
  • Strip bare URLs and domain CTAs
  • Drop ad / promo / subscribe / stream / visit lines entirely
  • Drop nutrition-label blocks
  • Drop "find it online", author bylines that are pure meta, rating widgets
  • Never allow "visit", "hbomax", "subscription required", etc. into
    ingredients, steps, title, or notes

If something looks like marketing, it dies here — loudly in logs/warnings,
silently in the user-facing card.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

# ── URL / domain patterns ────────────────────────────────────────────────────
URL_RE = re.compile(
    r"""(?xi)
    (?:https?://|www\.)[^\s<>"')\]]+   # full URLs
    | \b[a-z0-9][-a-z0-9]{1,30}\.(?:com|net|org|io|co|us|uk|info|biz|tv|me|app|shop)
      (?:/[^\s<>"')\]]*)?              # bare domains + path
    """
)

# Whole-line killers (if the line is mostly this junk, drop the line)
JUNK_LINE = re.compile(
    r"""(?xi)^\s*(?:
        (?:visit|click\s+here|learn\s+more|shop\s+now|buy\s+now|order\s+now|
           sign\s+up|sign\s*in|log\s*in|log\s*on|subscribe|stream\s+now|
           start\s+watching|enroll\s+now|
           advertisement|advert|sponsored|promo(?:tion)?|affiliate|
           jump\s+to\s+recipe|print\s+recipe|pin\s+(?:it|recipe)|save\s+recipe|
           rate\s+(?:this|recipe)|leave\s+a\s+comment|share\s+this|
           find\s+it\s+online|get\s+the\s+recipe|as\s+seen\s+on|
           subscription\s+required|plans?\s+start\s+at|stream\s+stream|
           skip\s+to\s+(?:content|recipe)|related\s+posts?|
           you\s+may\s+also\s+like|more\s+from|follow\s+us|
           newsletter|cookie\s+policy|privacy\s+policy|
           comment\s+more|copy\s+article|gift\s+this|click\s+to\s+expand|
           join\s+the\s+conversation|subscriber-only|read\s+comments|
           trending|manage\s+subscription|forgot\s+or\s+need\s+a\s+password|
           run\s+payroll|surepayroll|hbomax|netflix|disney\+|
           advertise|ingredientsad|___+
        )\b.*
        |
        (?:https?://|www\.)\S+          # line that is basically a URL
        |
        \S+\.(?:com|net|org|io)/\S*     # domain/path only line
    )\s*$"""
)

# Inline junk phrases to scrub out of otherwise-ok lines
INLINE_JUNK = re.compile(
    r"""(?xi)
    \b(?:visit|click\s+here|subscription\s+required|stream\s+now|
       sign\s+up|advertisement|sponsored\s+content|affiliate\s+link)\b
    (?:\s*[:\-–—]?\s*\S+)?
    """
)

# Nutrition / calorie blocks (common scraper pollution)
NUTRITION_LINE = re.compile(
    r"""(?xi)^\s*(?:
        nutrition(?:al)?\s*(?:info|facts|information)?|
        (?:serving\s+size|calories|sugar|sodium|fat|saturated|unsaturated|
           trans\s*fat|carbohydrates?|fiber|protein|cholesterol)\s*:?\s*[\d.]|
        \d+\s*(?:kcal|cal(?:ories)?)\b|
        find\s+it\s+online\s*:
    )"""
)

# Meta noise that is not food
META_LINE = re.compile(
    r"""(?xi)^\s*(?:
        (?:prep|cook|total)\s*time\s*:?\s*\d|
        (?:author|by|written\s+by|recipe\s+by|course|cuisine|keyword)s?\s*:|
        (?:updated|published|posted)\s*:?\s*\w|
        \d+(?:\.\d+)?\s*(?:from|stars?|ratings?)\b|
        (?:jump\s+to|print|share|pin|save|scale)\b|
        serves?\s+serves\b|          # "Serves Serves approximately"
        ingredients?\s*advertisement|
        instructions?\s*advertisement|
        stream\s+stream|sign\s+up\s+sign\s+up|
        plans?\s+start\s+at|\$\d+\.\d+/m
    )"""
)

# Ingredient / step lines that are pure garbage tokens
GARBAGE_TOKEN = re.compile(
    r"""(?xi)^\s*(?:
        one|two|three|visit|advertisement|advert|advertise|vertise|vertisement|
        sponsored|stream|now|sign\s*up|plans?|start\s*at|subscription|required|
        whip|ingredientsad|instructionsad|___+|
        \.+|\-+|\*+|•+
    )\s*$"""
)

# Domains we never want appearing anywhere in output
BANNED_DOMAIN_HINTS = re.compile(
    r"""(?xi)
    \b(?:hbomax|netflix|disney|hulu|primevideo|surepayroll|paychex|
       doubleclick|googlesyndication|outbrain|taboola|amazon-adsystem)\b
    """
)


def sanitize_recipe_text(text: str) -> Tuple[str, List[str]]:
    """
    Clean raw imported recipe text. Returns (cleaned_text, warnings).
    """
    if not text:
        return "", []
    warnings: List[str] = []
    out_lines: List[str] = []
    dropped = 0

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            # keep single blank separators
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue

        reason = _line_reject_reason(line)
        if reason:
            dropped += 1
            continue

        cleaned = scrub_inline(line)
        if not cleaned or _line_reject_reason(cleaned):
            dropped += 1
            continue
        out_lines.append(cleaned)

    # Collapse 3+ blanks
    text_out = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    # Final pass: annihilate any URL that survived
    if URL_RE.search(text_out) or BANNED_DOMAIN_HINTS.search(text_out):
        text_out2, n = URL_RE.subn("", text_out)
        text_out2 = BANNED_DOMAIN_HINTS.sub("", text_out2)
        text_out2 = re.sub(r"[ \t]{2,}", " ", text_out2)
        text_out2 = re.sub(r"\n{3,}", "\n\n", text_out2).strip()
        if n or text_out2 != text_out:
            warnings.append(f"Stripped URL/domain junk from body ({n} URL(s))")
            text_out = text_out2

    if dropped:
        warnings.append(f"Removed {dropped} ad/promo/junk line(s) from import")
    return text_out, warnings


def scrub_inline(text: str) -> str:
    """Remove URLs and visit-CTAs from a single string; keep real words."""
    if not text:
        return ""
    s = URL_RE.sub("", text)
    s = INLINE_JUNK.sub("", s)
    s = BANNED_DOMAIN_HINTS.sub("", s)
    # Common glued garbage from scrapers
    s = re.sub(r"(?i)\bADVERTISEMENT\b", "", s)
    s = re.sub(r"(?i)\bSTREAM\s+NOW\b", "", s)
    s = re.sub(r"(?i)\bSIGN\s+UP\b", "", s)
    s = re.sub(r"(?i)\bvisit\b", "", s)
    s = re.sub(r"(?i)find\s+it\s+online\s*:?\s*", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    return s.strip(" \t-–—|·•")


def is_junk_ingredient_line(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) < 2:
        return True
    if _line_reject_reason(s):
        return True
    if GARBAGE_TOKEN.match(s):
        return True
    # No food-like content: pure promo
    if URL_RE.search(s) or BANNED_DOMAIN_HINTS.search(s):
        return True
    if re.search(r"(?i)\b(visit|advertisement|subscribe|stream\s+now)\b", s):
        return True
    # Nutrition masquerading as ingredient
    if NUTRITION_LINE.match(s):
        return True
    return False


def is_junk_step(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    if _line_reject_reason(s):
        return True
    if GARBAGE_TOKEN.match(s):
        return True
    if URL_RE.search(s) or BANNED_DOMAIN_HINTS.search(s):
        return True
    if re.search(
        r"(?i)\b(visit|advertisement|subscribe|stream\s+now|sign\s+up|sign\s*in|"
        r"subscription\s+required|find\s+it\s+online|hbomax|netflix)\b",
        s,
    ):
        return True
    # Site chrome glued after the recipe ("Recipes Home Comment")
    if re.match(
        r"(?i)^(recipes?\s+home|home\s+comment|comment\s+\d+|recipes?\s*$|"
        r"more about|trending|by\s+[A-Z][a-z]+\s+[A-Z])",
        s,
    ):
        return True
    if re.search(r"(?i)\b(recipes?\s+home|comment\s+\d+|copy article link)\b", s):
        return True
    # Steps that are mostly non-alpha junk
    letters = sum(c.isalpha() for c in s)
    if letters < 8 and len(s) < 20:
        return True
    return False


def sanitize_title(title: str) -> str:
    t = scrub_inline(title or "")
    t = re.sub(r"(?i)\b(advertisement|visit|subscribe)\b", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -|")
    if not t or is_junk_step(t) or URL_RE.search(t):
        return "Untitled Recipe"
    # Never ship paywall chrome as a recipe name
    if re.match(
        r"(?i)^(sign\s*in|log\s*in|log\s*on|subscribe|register|join|comments?|"
        r"newsletter|trending|error|404|access\s+denied)\b",
        t,
    ):
        return "Untitled Recipe"
    return t


def sanitize_source_label(source: str) -> str:
    """
    Source is allowed to be a URL (user imported from web) but we never
    print marketing CTAs. For display in PDF footer, shorten to host only
    when it's a URL — never 'visit hbomax.com'.
    """
    s = (source or "").strip()
    if not s:
        return ""
    if re.search(r"(?i)\b(visit|advertisement|subscribe|stream)\b", s):
        return "web import"
    # Keep real recipe source URLs but strip tracking junk
    if s.startswith("http://") or s.startswith("https://"):
        # host only for safety/brevity in footer
        m = re.match(r"https?://([^/\s]+)", s)
        if m:
            host = m.group(1).lower()
            if BANNED_DOMAIN_HINTS.search(host):
                return "web import"
            return f"https://{host}/…"
        return "web import"
    # upload: long filename → short label
    if s.lower().startswith("upload:"):
        name = s.split(":", 1)[-1].strip()
        name = name.rsplit("/", 1)[-1]
        if len(name) > 40:
            name = name[:37] + "…"
        return f"upload: {name}" if name else "upload"
    # pasted text / example — fine
    if URL_RE.search(s) or BANNED_DOMAIN_HINTS.search(s):
        return "import"
    return s[:80]


def _line_reject_reason(line: str) -> Optional[str]:
    s = line.strip()
    if not s:
        return None
    if JUNK_LINE.match(s):
        return "junk_line"
    if NUTRITION_LINE.match(s):
        return "nutrition"
    # Allow real yield lines ("Serves 4", "Yield: 6 people") — strip scale widgets
    if re.match(r"(?i)^(serves?|servings?|yield|makes?)\s*:?", s):
        if re.search(r"(?i)serves\s+serves", s):
            return "meta_widget"
        # "Yield: Serves approximately 6 people 1x" → keep (converter cleans 1x)
        return None
    if META_LINE.match(s):
        if re.match(r"(?i)^(author|by|course|cuisine|keyword)", s):
            return "meta"
        return "meta"
    if GARBAGE_TOKEN.match(s):
        return "garbage_token"
    if BANNED_DOMAIN_HINTS.search(s):
        return "banned_domain"
    # Line is mostly a URL
    no_url = URL_RE.sub("", s).strip()
    if URL_RE.search(s) and len(no_url) < 8:
        return "url_only"
    # "Visit hbomax.com" style
    if re.match(r"(?i)^visit\b", s):
        return "visit_cta"
    if re.search(r"(?i)\bvisit\s+\S+\.(com|net|org)\b", s):
        return "visit_cta"
    # Ad glue tokens
    if re.search(r"(?i)ingredients?ad|instructions?ad", s):
        return "ad_glue"
    if s.count("ADVERTISEMENT") or s.lower() == "advertisement":
        return "advertisement"
    if re.search(r"(?i)\$\d+\.\d+\s*/\s*m\b", s):
        return "subscription_price"
    return None


def assert_clean_output(text: str, where: str = "output") -> List[str]:
    """
    Final guard before PDF/HTML. Returns list of problems found (and caller
    should scrub). Used as a scream-test so bugs don't ship quietly.
    """
    problems = []
    if not text:
        return problems
    if URL_RE.search(text):
        problems.append(f"{where}: contains URL")
    if BANNED_DOMAIN_HINTS.search(text):
        problems.append(f"{where}: contains banned domain")
    if re.search(r"(?i)\bvisit\b", text):
        problems.append(f"{where}: contains 'visit'")
    if re.search(r"(?i)\badvertisement\b", text):
        problems.append(f"{where}: contains 'advertisement'")
    if re.search(r"(?i)\bsubscription\s+required\b", text):
        problems.append(f"{where}: contains subscription CTA")
    if re.search(r"(?i)\bstream\s+now\b", text):
        problems.append(f"{where}: contains stream promo")
    return problems
