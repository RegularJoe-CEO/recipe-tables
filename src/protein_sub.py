"""
Protein substitution for TRN tables.

Logic:
  1. Identify ingredient rows marked is_protein (or matching the proteins list).
  2. Replace the protein name in those ingredient rows with the chosen substitute,
     preserving quantities and prep notes.
  3. Rewrite step detail/action text: swap original protein tokens (and common
     aliases) for the new protein, case-aware, longest-match, non-overlapping.
  4. Record original_protein / substituted_protein on the table for the PDF meta line.

The protein dropdown list lives in data/proteins.json and is user-editable
from the Streamlit settings sidebar (or by editing the JSON file directly).
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Optional

from .models import TRNTable

# Default path relative to project root
_DEFAULT_PROTEINS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "proteins.json"
)


def load_proteins(path: Optional[str] = None) -> dict:
    path = path or _DEFAULT_PROTEINS_PATH
    if not os.path.isfile(path):
        return {
            "proteins": [
                "chicken",
                "beef",
                "pork",
                "turkey",
                "tofu",
                "tempeh",
                "shrimp",
                "salmon",
            ],
            "aliases": {},
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_proteins(data: dict, path: Optional[str] = None) -> None:
    path = path or _DEFAULT_PROTEINS_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def list_proteins(path: Optional[str] = None) -> list[str]:
    return list(load_proteins(path).get("proteins") or [])


def add_protein(name: str, path: Optional[str] = None) -> list[str]:
    name = name.strip().lower()
    if not name:
        return list_proteins(path)
    data = load_proteins(path)
    prots = data.setdefault("proteins", [])
    if name not in [p.lower() for p in prots]:
        prots.append(name)
        save_proteins(data, path)
    return list_proteins(path)


def detect_proteins_in_table(table: TRNTable, path: Optional[str] = None) -> list[str]:
    """Return protein names found in the table's ingredients."""
    data = load_proteins(path)
    known = [p.lower() for p in data.get("proteins") or []]
    aliases = {k.lower(): v.lower() for k, v in (data.get("aliases") or {}).items()}
    found = []
    for ing in table.ingredients:
        n = ing.name.lower()
        if ing.is_protein:
            found.append(ing.name)
            continue
        matched = False
        for alias, _canon in aliases.items():
            if alias in n:
                found.append(ing.name)
                matched = True
                break
        if matched:
            continue
        for p in known:
            if re.search(rf"\b{re.escape(p)}\b", n):
                found.append(ing.name)
                break
    seen = set()
    out = []
    for f in found:
        if f.lower() not in seen:
            seen.add(f.lower())
            out.append(f)
    return out


def substitute_protein(
    table: TRNTable,
    new_protein: str,
    original: Optional[str] = None,
    path: Optional[str] = None,
) -> TRNTable:
    """
    Return a deep-copied table with protein swapped.

    If `original` is None, uses the first detected protein ingredient.

    Replacement is longest-match / non-overlapping so that
    "chicken breast" becomes "tofu" once — never "tofu tofu".
    Only known protein words and aliases are replaceable; cut words
    like "breast" / "boneless" are stripped from ingredient names after
    the swap rather than being treated as protein tokens.
    """
    t = deepcopy(table)
    new_protein = new_protein.strip()
    if not new_protein:
        return t

    data = load_proteins(path)
    aliases = {k.lower(): v for k, v in (data.get("aliases") or {}).items()}

    detected = detect_proteins_in_table(t, path)
    if original is None:
        original = t.original_protein or (detected[0] if detected else None)
    if not original:
        t.substituted_protein = new_protein
        return t

    t.original_protein = t.original_protein or original
    t.substituted_protein = new_protein

    known = [p.lower() for p in data.get("proteins") or []]
    originals: set[str] = {original.lower().strip()}

    for p in known:
        if re.search(rf"\b{re.escape(p)}\b", original, re.I):
            originals.add(p)

    for alias, canon in aliases.items():
        alias_l = alias.lower()
        canon_l = str(canon).lower()
        related = (
            alias_l in original.lower()
            or any(re.search(rf"\b{re.escape(o)}\b", alias_l) for o in originals)
            or any(re.search(rf"\b{re.escape(o)}\b", canon_l) for o in originals)
            or canon_l in originals
        )
        if related:
            originals.add(alias_l)
            if canon_l in known or any(
                re.search(rf"\b{re.escape(canon_l)}\b", o) for o in originals
            ):
                originals.add(canon_l)

    patterns = sorted(originals, key=len, reverse=True)

    def replace_in_text(text: str) -> str:
        if not text:
            return text

        def casing(span: str) -> str:
            if span.isupper():
                return new_protein.upper()
            if span[0].isupper():
                return new_protein[:1].upper() + new_protein[1:]
            return new_protein.lower()

        claimed = [False] * len(text)
        hits: list[tuple[int, int, str]] = []
        for pat in patterns:
            if len(pat) < 3 and pat not in {"ham"}:
                continue
            for m in re.finditer(rf"\b{re.escape(pat)}\b", text, flags=re.IGNORECASE):
                a, b = m.span()
                if any(claimed[a:b]):
                    continue
                for i in range(a, b):
                    claimed[i] = True
                hits.append((a, b, casing(m.group(0))))

        if not hits:
            return text
        hits.sort(key=lambda x: x[0])
        parts: list[str] = []
        cursor = 0
        for a, b, rep in hits:
            parts.append(text[cursor:a])
            parts.append(rep)
            cursor = b
        parts.append(text[cursor:])
        return "".join(parts)

    cut_words = re.compile(
        r"\b(breasts?|thighs?|fillets?|cutlets?|tenderloins?|chops?|"
        r"strips?|boneless|skinless)\b",
        re.I,
    )

    for ing in t.ingredients:
        is_match = ing.is_protein or any(
            re.search(rf"\b{re.escape(p)}\b", ing.name, re.I) for p in patterns
        )
        if is_match:
            new_name = replace_in_text(ing.name)
            simplified = cut_words.sub("", new_name)
            simplified = re.sub(r"\s{2,}", " ", simplified).strip(" ,")
            if (
                not simplified
                or simplified.lower() == new_protein.lower()
                or simplified.lower() in {p.lower() for p in patterns}
            ):
                ing.name = new_protein
            else:
                ing.name = simplified
            ing.is_protein = True

    for step in t.steps:
        step.detail = replace_in_text(step.detail)
        step.action = replace_in_text(step.action)

    return t
