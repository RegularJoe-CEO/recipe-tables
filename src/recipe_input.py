"""
Recipe input adapters: plain text, URL scrape, PDF/image OCR.

URL path always tries to capture a real recipe title (JSON-LD name, og:title,
h1, page <title>) so the TRN card is never "Untitled" when a name is available.
"""

from __future__ import annotations

import io
import json
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .sanitize import sanitize_recipe_text, sanitize_title, scrub_inline
from .text_cleanup import cleanup_recipe_text

# Return type: (raw_text, source_label, title_or_none, fetch_method)
# fetch_method: paste | jsonld | html_scrape | reader | upload | ocr
LoadResult = Tuple[str, str, Optional[str], str]


def load_from_text(text: str) -> LoadResult:
    """Return (raw_text, source_label, title_hint, fetch_method) after sanitizing junk."""
    cleaned = cleanup_recipe_text(text.strip())
    cleaned, _ = sanitize_recipe_text(cleaned)
    return cleaned, "pasted text", None, "paste"


def load_from_url(url: str, timeout: int = 25) -> LoadResult:
    """
    Fetch a recipe URL despite ads and bot blocks.

    Engineering strategy (order matters):
      1. Browser-like direct GET
      2. Prefer schema.org Recipe JSON-LD from HTML (ads never appear there)
      3. Else scrape main recipe containers (still messy → LLM cleans later)
      4. On 403/401/429/empty/junk → public reader (r.jina.ai)
      5. Clear error if both fail (PDF / paste always work)

    Title priority: JSON-LD name → og:title → h1 → <title>
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    html, fetch_err = _http_get_html(url, timeout=timeout)

    if html and len(html) > 200:
        try:
            body, source, title, method = _parse_html_recipe(html, url)
            # If HTML parse only got a paywall shell, fall through to reader
            if body and not _looks_like_paywall_or_shell(body, title):
                return body, source, title, method
            fetch_err = fetch_err or "html looked like paywall/shell"
        except Exception as e:
            fetch_err = f"parse failed after fetch: {e}"

    # Site blocked us or returned unusable HTML — try reader proxy
    reader_body, reader_title, reader_err = _fetch_via_reader(url, timeout=timeout)
    if reader_body and len(reader_body.strip()) > 80:
        body = cleanup_recipe_text(reader_body)
        body, _ = sanitize_recipe_text(body)
        title = _clean_title(reader_title) if reader_title else _clean_title(
            _extract_title_from_body(body)
        )
        # Reject paywall / sign-in shells even after cleanup
        if title and _BAD_TITLES.match(title):
            title = _clean_title(_extract_title_from_body(body))
        if _looks_like_paywall_or_shell(body, title):
            reader_err = "got sign-in/paywall page instead of recipe"
        else:
            if title:
                body = _ensure_title_first(body, title)
            # Reader text is almost always chrome-heavy — LLM extract is the fix
            return body, url, title, "reader"

    # Both failed
    host = urlparse(url).netloc or url
    detail = fetch_err or reader_err or "unknown error"
    raise RuntimeError(
        f"Could not fetch a clean recipe from {host} ({detail}).\n\n"
        "The site may require a subscription or returned a Sign In page.\n\n"
        "**Workarounds that always work:**\n"
        "1. Open the page in your browser → Print → Save as PDF → **Upload PDF** tab\n"
        "2. Select only the ingredients + instructions → Copy → **Paste text** tab\n"
        "3. Use a PDF you already saved (e.g. Masa Biscuits - Texas Monthly.pdf)\n\n"
        "With **XAI_API_KEY** set, messy pages that *do* fetch are cleaned by LLM "
        "before TRN — ads never enter the table."
    )


def _browser_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _http_get_html(url: str, timeout: int = 25) -> Tuple[Optional[str], Optional[str]]:
    """Return (html, error_message)."""
    try:
        session = requests.Session()
        session.headers.update(_browser_headers())
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code in (401, 403, 429):
            return None, f"HTTP {resp.status_code} Forbidden/blocked"
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        if len(resp.text or "") < 200:
            return None, "empty response"
        return resp.text, None
    except requests.RequestException as e:
        return None, str(e)


def _fetch_via_reader(
    url: str, timeout: int = 45
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Fallback for bot-blocked sites: Jina AI reader returns readable markdown.
    Free public endpoint — no API key required for light use.
    Returns (body_text, title, error).
    """
    reader_url = f"https://r.jina.ai/{url}"
    try:
        resp = requests.get(
            reader_url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; RecipeTables/1.0)",
                "Accept": "text/plain,text/markdown,*/*",
            },
        )
        if resp.status_code >= 400:
            return None, None, f"reader HTTP {resp.status_code}"
        text = resp.text or ""
        if len(text) < 100:
            return None, None, "reader empty"
        title, body = _parse_reader_markdown(text)
        if not body or _looks_like_paywall_or_shell(body, title):
            return None, None, "reader returned sign-in/paywall/junk page"
        return body, title, None
    except requests.RequestException as e:
        return None, None, str(e)


# Titles that mean we scraped a wall, chrome, or a non-recipe heading
_BAD_TITLES = re.compile(
    r"(?i)^(sign\s*in|log\s*in|log\s*on|subscribe|join|register|create\s+account|"
    r"access\s+denied|page\s+not\s+found|404|error|cookie|privacy|"
    r"newsletter|comments?|trending|"
    r"equipment|tools?|special\s+equipment|what\s+you.?ll\s+need|"
    r"ingredients?|directions?|instructions?|method|steps?|notes?|"
    r"nutrition|servings?|yield|print|share)\b"
)

# After the real recipe ends, magazine pages dump related content
_RECIPE_END_MARKERS = re.compile(
    r"(?im)^(?:#{0,4}\s*)?(?:"
    r"comments?|read\s+comments|join\s+the\s+conversation|"
    r"sign\s*in|log\s*in|subscribe|newsletter|food\s*&\s*drink|"
    r"more\s+(?:texas\s+monthly\s+)?recipes|recommended\s+recipes|"
    r"related\s+(?:recipes|articles|posts)|you\s+may\s+also\s+like|"
    r"get\s+our\s+next|email\s+address|trending|copy\s+article|"
    r"gift\s+this\s+article|subscriber-only|already\s+have\s+a\s+subscription|"
    r"privacy\s+policy|terms\s+of\s+use|do\s+not\s+sell|"
    r"manage\s+subscription|copyright|"
    r"we\s+report\s+on\s+vital|news\s*&\s*politics|"
    # Lone chrome lines after "Serve warm." on magazine pages
    r"recipes\s*$|home\s*$|comment\s+\d+|print\s*$"
    r")"
)


def _looks_like_paywall_or_shell(body: str, title: Optional[str]) -> bool:
    """True if content is a sign-in wall or page chrome without a real recipe."""
    if title and _BAD_TITLES.match(title.strip()):
        return True
    low = (body or "").lower()
    if re.search(r"(?i)^sign\s*in\s*$", (body or "").strip().split("\n")[0] or ""):
        return True
    # Must have some ingredient-like lines
    qty_lines = sum(
        1
        for ln in (body or "").split("\n")
        if re.match(r"^[\-\*]?\s*[\d½¼¾⅓⅔]", ln.strip())
        or re.search(r"\b(cup|tbsp|tsp|oz|lb|tablespoon|teaspoon)\b", ln, re.I)
    )
    if qty_lines < 2:
        return True
    # Too much paywall language vs food
    paywall_hits = len(
        re.findall(
            r"(?i)\b(sign\s*in|log\s*in|subscription|newsletter|password|facebook|google)\b",
            low,
        )
    )
    if paywall_hits > 8 and qty_lines < 5:
        return True
    return False


def _parse_reader_markdown(md: str) -> Tuple[Optional[str], str]:
    """
    Convert Jina-style markdown into plain recipe text for our converter.

    Critical: only keep the recipe card block (title → ingredients → directions).
    Cut everything after: comments, related articles, sign-in, newsletter.
    Never take "Sign In" from the page footer as the recipe title.
    """
    # ── Locate Ingredients heading (the real recipe card) ────────────────
    ing_match = None
    for marker in (
        r"(?im)^#{1,4}\s+ingredients?\s*$",
        r"(?im)^\*\*ingredients?\*\*\s*$",
        r"(?im)^ingredients?\s*$",
    ):
        ing_match = re.search(marker, md)
        if ing_match:
            break

    if not ing_match:
        # No ingredients section — likely not a usable recipe page
        return None, ""

    # Include a short window before ingredients for title + servings
    # (but not so far we grab an earlier "Equipment" section as the title)
    pre_start = max(0, ing_match.start() - 500)
    chunk = md[pre_start:]

    # Drop Equipment/Tools lists so "1 smoker" never becomes an ingredient
    chunk = re.sub(
        r"(?is)(?:^|\n)(?:#{1,4}\s*)?(?:equipment|tools?|special\s+equipment)\s*\n.*?"
        r"(?=\n(?:#{1,4}\s*)?(?:ingredients?|directions?|instructions?|method)\b)",
        "\n",
        chunk,
    )

    # Normalize markdown BEFORE end-cutting so "[Comment](url)" / "* Recipes"
    # match our end markers cleanly
    chunk = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", chunk)
    chunk = re.sub(r"!\[([^\]]*)\]\([^)]+\)", "", chunk)
    chunk = re.sub(r"^\s*[\*\-]\s+", "", chunk, flags=re.M)

    # ── Cut at end-of-recipe markers (related articles, sign-in, etc.) ───
    dir_match = None
    for marker in (
        r"(?im)^#{1,4}\s+(?:directions?|instructions?|method|steps?|preparation)\s*$",
        r"(?im)^\*\*(?:directions?|instructions?|method)\*\*\s*$",
        r"(?im)^(?:directions?|instructions?|method)\s*$",
    ):
        dir_match = re.search(marker, chunk)
        if dir_match:
            break

    search_from = dir_match.end() if dir_match else max(0, ing_match.end() - pre_start)

    # Hard cut: first share/chrome/comment block after directions
    hard_end = re.search(
        r"(?im)^(?:more about|share on|comment|recipes\s*$|home\s*$|"
        r"print this article|copy article|gift this|click to expand|"
        r"sign in|log in|subscribe|newsletter)",
        chunk[search_from:],
    )
    if hard_end:
        chunk = chunk[: search_from + hard_end.start()]

    end_match = _RECIPE_END_MARKERS.search(chunk, pos=search_from)
    if end_match:
        chunk = chunk[: end_match.start()]

    # Also hard-stop if we see a long run of "By Author" article teasers
    by_articles = list(
        re.finditer(r"(?im)^#{0,4}\s*.{10,120}\s+By\s+[A-Z][a-z]+", chunk)
    )
    if len(by_articles) >= 2:
        after_dir = dir_match.end() if dir_match else 0
        for bm in by_articles:
            if bm.start() > after_dir + 80:
                chunk = chunk[: bm.start()]
                break

    # ── Title: heading immediately before Ingredients only ───────────────
    # Prefer food-like names ("Dr Pepper-Glazed Lamb…"), never "Equipment".
    title = None
    pre_ing = (
        chunk[: chunk.lower().find("ingredient")]
        if "ingredient" in chunk.lower()
        else chunk[:400]
    )
    candidates: list[str] = []
    for h in re.findall(r"(?m)^#{1,3}\s+(.+)$", pre_ing):
        candidates.append(h.strip())
    for ln in pre_ing.strip().split("\n"):
        s = ln.strip("# *-").strip()
        if s:
            candidates.append(s)
    # Score candidates: later + food-like beats chrome
    best_score = -1
    for s in candidates:
        if not s or len(s) < 3 or len(s) > 100:
            continue
        if _BAD_TITLES.match(s):
            continue
        if re.search(r"(?i)accessib|screen-reader|newsletter|print recipe", s):
            continue
        if re.match(r"(?i)^(servings?|yield|print|use these|this |a classic)", s):
            continue
        score = 10
        if re.search(
            r"(?i)\b(chicken|beef|pork|lamb|fish|shrimp|pasta|soup|salad|"
            r"cake|cookie|biscuit|bread|taco|bbq|smoked|glazed|roast|"
            r"pepper|garlic|butter|masa|pie|stew|chili)\b",
            s,
        ):
            score += 20
        if re.search(r"(?i)\b(equipment|smoker|grill|tool)\b", s) and len(s) < 20:
            score -= 30
        if score > best_score:
            best_score = score
            title = s
    # Jina "Title:" metadata as last resort
    m = re.search(r"(?m)^Title:\s*(.+)$", md)
    if m and (not title or best_score < 15):
        cand = m.group(1).strip()
        if not _BAD_TITLES.match(cand) and len(cand) < 100:
            if not title:
                title = cand

    # ── Clean lines ──────────────────────────────────────────────────────
    lines = []
    for ln in chunk.split("\n"):
        s = ln.strip()
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if s.startswith("![") or re.match(r"^\[!\[", s):
            continue
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^\*\s+", "- ", s)
        if re.match(r"^(1x|2x|3x)(\s+(1x|2x|3x))*\s*$", s, re.I):
            continue
        if re.match(r"(?i)^(print recipe|copy article|gift this|click to expand)", s):
            continue
        if _BAD_TITLES.match(s):
            continue
        if re.match(r"(?i)^(by\s+[A-Z]|comment\s+\d|read comments)", s):
            continue
        if re.search(r"(?i)\b(sign in to your|already have a subscription|log in\.)\b", s):
            continue
        # Drop "More Texas Monthly Recipes" chrome
        if re.search(r"(?i)more about:|more texas monthly|recommended recipes", s):
            continue
        lines.append(s)

    text = "\n".join(lines)
    text = re.sub(
        r"(?im)^servings?\s*\[?(\d+)\]?\s*(biscuits?|cookies?|people|servings?)?",
        r"Servings: \1 \2",
        text,
    )
    # Ensure Ingredients / Directions headers exist for the converter
    if not re.search(r"(?im)^ingredients?\b", text):
        # Insert before first qty line
        text2, n = re.subn(
            r"(?m)^(?=[\d½¼¾⅓⅔1-9])",
            "Ingredients\n",
            text,
            count=1,
        )
        if n:
            text = text2
    if re.search(r"(?im)^ingredients?\b", text) and not re.search(
        r"(?im)^(?:directions?|instructions?|method)\b", text
    ):
        text = re.sub(
            r"(?im)^("
            r"heat your oven|preheat|in a medium|combine the dry|add the|"
            r"combine\s|mix\s|rub\s|heat a smoker|heat a grill"
            r")",
            r"Directions\n\1",
            text,
            count=1,
        )

    # Final title safety
    if title and _BAD_TITLES.match(title):
        title = None
    if title and not text.lower().lstrip().startswith(title.lower()[: min(20, len(title))].lower()):
        text = f"{title}\n\n{text}"

    return title, text.strip()


def _extract_title_from_body(body: str) -> Optional[str]:
    for ln in (body or "").split("\n"):
        s = ln.strip()
        if not s or _BAD_TITLES.match(s):
            continue
        if re.match(r"^(ingredients?|directions?|servings?)", s, re.I):
            continue
        if 3 < len(s) < 100:
            return s
    return None


def _parse_html_recipe(html: str, url: str) -> LoadResult:
    """Parse direct HTML into (body, source, title, fetch_method)."""
    soup = BeautifulSoup(html, "lxml")

    # ── Title + recipe body from JSON-LD FIRST (before stripping scripts) ──
    # This is the engineering win on ad-heavy blogs: schema.org Recipe is
    # structured food data only — no ads, no "Sign In", no related posts.
    jsonld_title, jsonld_text = _extract_jsonld_recipe(soup)
    page_title = _extract_page_title(soup, fallback_url=url)
    title = _clean_title(jsonld_title or page_title)

    if jsonld_text and len(jsonld_text.strip()) > 80:
        body = jsonld_text.strip()
        if title and not body.lower().startswith(title.lower()[:20].lower()):
            body = f"{title}\n\n{body}"
        body = cleanup_recipe_text(body)
        body, _ = sanitize_recipe_text(body)
        return body, url, title, "jsonld"

    # ── HTML fallback (will often need LLM extract downstream) ───────────
    h1_title = None
    for h1 in soup.find_all("h1"):
        t = h1.get_text(" ", strip=True)
        if t and 3 < len(t) < 120 and not _looks_like_nav(t):
            h1_title = t
            break
    if not title and h1_title:
        title = _clean_title(h1_title)
    elif h1_title and title and len(h1_title) < len(title):
        title = _clean_title(h1_title)

    for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "form"]):
        tag.decompose()

    body_text = None
    for sel in (
        "div.wprm-recipe-container",
        "div.tasty-recipes",
        "div.easyrecipe",
        "div.recipe",
        "article.recipe",
        "[itemtype*='Recipe']",
        "article",
        "main",
    ):
        node = soup.select_one(sel)
        if node:
            text = node.get_text("\n", strip=True)
            if len(text) > 120:
                body_text = _clean_scraped(text)
                break

    if not body_text:
        body_text = _clean_scraped(soup.get_text("\n", strip=True))

    if title:
        body_text = _ensure_title_first(body_text, title)

    body_text = cleanup_recipe_text(body_text)
    body_text, _ = sanitize_recipe_text(body_text)
    return body_text, url, title, "html_scrape"


def _extract_jsonld_recipe(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    """Return (title, formatted_text) from schema.org Recipe JSON-LD."""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Some sites emit trailing commas / multiple objects
            try:
                raw2 = re.sub(r",\s*}", "}", raw)
                raw2 = re.sub(r",\s*]", "]", raw2)
                data = json.loads(raw2)
            except Exception:
                continue

        for node in _walk_jsonld(data):
            types = node.get("@type", "")
            if isinstance(types, list):
                types = " ".join(str(t) for t in types)
            if "Recipe" not in str(types):
                continue
            title = node.get("name")
            if isinstance(title, list):
                title = title[0] if title else None
            text = _recipe_from_jsonld(node)
            return (str(title).strip() if title else None), text
    return None, None


def _walk_jsonld(data) -> list:
    """Flatten JSON-LD into a list of dict nodes (@graph, arrays, nested)."""
    out = []
    if isinstance(data, list):
        for item in data:
            out.extend(_walk_jsonld(item))
    elif isinstance(data, dict):
        if "@graph" in data:
            out.extend(_walk_jsonld(data["@graph"]))
        else:
            out.append(data)
            # Some pages nest Recipe under mainEntity
            for key in ("mainEntity", "mainEntityOfPage"):
                if key in data:
                    out.extend(_walk_jsonld(data[key]))
    return out


def _extract_page_title(soup: BeautifulSoup, fallback_url: str = "") -> Optional[str]:
    """og:title → twitter:title → <title>."""
    for prop in (
        ("property", "og:title"),
        ("name", "twitter:title"),
        ("itemprop", "name"),
    ):
        tag = soup.find("meta", attrs={prop[0]: prop[1]})
        if tag and tag.get("content"):
            return tag["content"].strip()

    if soup.title and soup.title.string:
        return soup.title.string.strip()

    # Last resort: slug from URL path
    try:
        path = urlparse(fallback_url).path.strip("/").split("/")[-1]
        if path and path not in {"recipe", "recipes", "index.html"}:
            guess = re.sub(r"[-_]+", " ", path)
            guess = re.sub(r"\.\w+$", "", guess)
            if len(guess) > 3:
                return guess.title()
    except Exception:
        pass
    return None


def _clean_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = title.strip()
    # Strip common site suffixes: "Recipe Name | Site", "Recipe Name - Site"
    t = re.split(r"\s+[|\u2013\u2014\-]\s+(?:recipe|recipes|the\s+kitchn|allrecipes|food\s+network|simply\s+recipes|bon\s+appetit|seriouseats|cooking\s+light|delish|tasty|budget\s+bytes).*$", t, flags=re.I)[0]
    t = re.split(r"\s+[|]\s+", t)[0].strip()
    # "Recipe Name - The Kitchn" style
    t = re.sub(
        r"\s+[-–—]\s+(the\s+kitchn|allrecipes|food\s+network|nytimes|bbc|epicurious).*$",
        "",
        t,
        flags=re.I,
    ).strip()
    # Remove trailing "Recipe" duplication noise only if very long
    if len(t) > 80:
        t = t[:77].rstrip() + "…"
    if len(t) < 2:
        return None
    return t


def _ensure_title_first(body: str, title: str) -> str:
    """Guarantee the recipe name is the first non-empty line of body text."""
    body = (body or "").strip()
    if not title:
        return body
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    if not lines:
        return title
    first = lines[0]
    # Already starts with the title (or title is a prefix)
    if first.lower() == title.lower() or first.lower().startswith(title.lower()[: min(24, len(title))]):
        # Replace first line with clean title if first line is dirtier
        if first != title and title.lower() in first.lower():
            lines[0] = title
        return "\n".join(lines)
    # First line is junk (ratings, "jump to recipe", etc.) — prepend title
    if _looks_like_nav(first) or _looks_like_meta_line(first):
        # drop leading junk lines
        while lines and (_looks_like_nav(lines[0]) or _looks_like_meta_line(lines[0])):
            lines.pop(0)
        return title + "\n\n" + "\n".join(lines)
    return title + "\n\n" + body


def _looks_like_nav(s: str) -> bool:
    low = s.lower().strip()
    return low in {
        "share",
        "pin",
        "print",
        "jump to recipe",
        "jump to",
        "scale",
        "save",
        "rate",
        "comment",
        "comments",
        "recipe",
        "recipes",
        "home",
        "skip to content",
        "skip to recipe",
    } or bool(re.match(r"^(home|recipes?|dinner|lunch|breakfast)\s*[>»/]", low))


def _looks_like_meta_line(s: str) -> bool:
    """Ratings, bylines, dates — not a recipe title."""
    low = s.lower()
    if re.match(r"^\d+(\.\d+)?\s*(from|stars?|ratings?)", low):
        return True
    if re.match(r"^(by|written by|recipe by|from)\s+\w+", low):
        return True
    if re.match(r"^(updated|published|posted)\b", low):
        return True
    if re.match(r"^\d+\s*(servings?|mins?|minutes?|hrs?|hours?|kcal|cal)\b", low):
        return True
    if re.match(r"^(prep|cook|total)\s*time\b", low):
        return True
    return False


def _recipe_from_jsonld(c: dict) -> str:
    parts = []
    name = c.get("name")
    if isinstance(name, list):
        name = name[0] if name else None
    if name:
        parts.append(str(name).strip())
    yield_ = c.get("recipeYield") or c.get("yield")
    if yield_:
        if isinstance(yield_, list):
            yield_ = yield_[0]
        parts.append(f"Servings: {yield_}")
    parts.append("")
    parts.append("Ingredients")
    ings = c.get("recipeIngredient") or c.get("ingredients") or []
    if isinstance(ings, str):
        ings = [ings]
    for ing in ings:
        parts.append(f"- {ing}")
    parts.append("")
    parts.append("Instructions")
    instr = c.get("recipeInstructions") or []
    if isinstance(instr, str):
        parts.append(instr)
    elif isinstance(instr, list):
        for i, step in enumerate(instr, 1):
            if isinstance(step, str):
                parts.append(f"{i}. {step}")
            elif isinstance(step, dict):
                text = step.get("text") or step.get("name") or ""
                if step.get("@type") == "HowToSection":
                    parts.append(str(step.get("name") or "Section"))
                    for sub in step.get("itemListElement") or []:
                        if isinstance(sub, dict):
                            parts.append(f"- {sub.get('text') or sub.get('name') or ''}")
                        else:
                            parts.append(f"- {sub}")
                elif text:
                    parts.append(f"{i}. {text}")
    return "\n".join(parts)


def _clean_scraped(text: str) -> str:
    lines = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if _looks_like_nav(s):
            continue
        lines.append(s)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out.strip()


def load_from_upload(file_bytes: bytes, filename: str) -> LoadResult:
    """
    OCR or extract text from an uploaded PDF or image.
    Returns (text, source, title_hint, fetch_method). Title may be None.
    Magazine/blog PDFs are usually messy → LLM extract cleans them.
    """
    name = (filename or "upload").lower()
    source = f"upload: {filename}"

    if name.endswith(".txt") or name.endswith(".md"):
        text = file_bytes.decode("utf-8", errors="replace")
        return _finalize_upload_text(text), source, None, "upload"

    if name.endswith(".pdf"):
        text = _pdf_text(file_bytes)
        if text and len(text.strip()) > 40:
            return _finalize_upload_text(text), source, None, "upload"
        ocr_text = _pdf_ocr(file_bytes)
        if ocr_text.strip() and not ocr_text.startswith("[OCR"):
            return _finalize_upload_text(ocr_text), source, None, "ocr"
        return _finalize_upload_text(text or ""), source, None, "upload"

    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif")):
        return _finalize_upload_text(_image_ocr(file_bytes)), source, None, "ocr"

    try:
        return _finalize_upload_text(file_bytes.decode("utf-8")), source, None, "upload"
    except Exception:
        return _finalize_upload_text(_image_ocr(file_bytes)), source, None, "ocr"


def _finalize_upload_text(text: str) -> str:
    """PDF/OCR cleanup + ad sanitizer before conversion."""
    if not text or text.startswith("[OCR") or text.startswith("[Image OCR"):
        return text
    text = cleanup_recipe_text(text)
    text, _ = sanitize_recipe_text(text)
    return text


def _pdf_text(file_bytes: bytes) -> str:
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return ""


def _pdf_ocr(file_bytes: bytes) -> str:
    try:
        from pdf2image import convert_from_bytes
        import pytesseract

        images = convert_from_bytes(file_bytes, dpi=200)
        parts = []
        for img in images:
            parts.append(pytesseract.image_to_string(img))
        return "\n".join(parts)
    except Exception as e:
        return (
            f"[OCR unavailable: {e}]\n"
            "Install Tesseract OCR and poppler for PDF OCR support.\n"
            "macOS: brew install tesseract poppler\n"
            "Ubuntu: sudo apt install tesseract-ocr poppler-utils\n"
            "Windows: install Tesseract from UB Mannheim builds."
        )


def _image_ocr(file_bytes: bytes) -> str:
    try:
        from PIL import Image
        import pytesseract

        img = Image.open(io.BytesIO(file_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        return pytesseract.image_to_string(img)
    except Exception as e:
        return (
            f"[Image OCR failed: {e}]\n"
            "Ensure Tesseract is installed and on PATH.\n"
            "macOS: brew install tesseract"
        )
