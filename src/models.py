"""
Data models for Recipe Tables / Tabular Recipe Notation (TRN).

TRN (Michael Chu, Cooking for Engineers, 2004) structure:
  - Left column: ingredients with measurements, in order of first use
  - Right columns: chronological cooking steps
  - Cells merge (rowspan/colspan conceptually) when ingredients combine
  - Top header rows: whole-dish prep actions (preheat oven, grease pan, …)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import json
import re
import uuid
from datetime import datetime, timezone


@dataclass
class Quantity:
    """A measured amount that can be scaled."""

    amount: Optional[float]  # None for unmeasured ("to taste", "pinch")
    unit: str = ""  # e.g. "cup", "tbsp", "g", ""
    raw: str = ""  # original text fragment, for fallback display
    note: str = ""  # "divided", "softened", etc.

    def scaled(self, factor: float) -> "Quantity":
        if self.amount is None:
            return Quantity(amount=None, unit=self.unit, raw=self.raw, note=self.note)
        return Quantity(
            amount=round(self.amount * factor, 4),
            unit=self.unit,
            raw=self.raw,
            note=self.note,
        )

    def display(self, factor: float = 1.0) -> str:
        """Amount + unit only (prep notes render after the ingredient name)."""
        q = self.scaled(factor) if factor != 1.0 else self
        if q.amount is None:
            return q.raw or q.unit or ""
        # Ranges ("10-12", "2-3") keep both ends; amount is only the midpoint
        rng = _RANGE_RE.match(self.raw or "")
        if rng:
            lo, hi = (_simple_number(g) for g in rng.groups())
            if lo is not None and hi is not None:
                sep = " to " if re.search(r"\bto\b", self.raw or "", re.I) else "-"
                parts = [f"{_format_amount(lo * factor)}{sep}{_format_amount(hi * factor)}"]
                if q.unit:
                    parts.append(q.unit)
                return " ".join(parts)
        amt = _format_amount(q.amount)
        parts = [amt]
        if q.unit:
            parts.append(q.unit)
        return " ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Quantity":
        return cls(**{k: d.get(k) for k in ("amount", "unit", "raw", "note")})


_RANGE_RE = re.compile(
    r"^\s*(\d+(?:\s+\d+/\d+|/\d+|\.\d+)?)\s*(?:[-–]|to)\s*(\d+(?:\s+\d+/\d+|/\d+|\.\d+)?)\s*$",
    re.I,
)


def _simple_number(token: str) -> Optional[float]:
    token = token.strip()
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)$", token)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.match(r"^(\d+)/(\d+)$", token)
    if m:
        return int(m.group(1)) / int(m.group(2))
    try:
        return float(token)
    except ValueError:
        return None


def _format_amount(n: float) -> str:
    """Pretty-print amounts with ASCII-safe fractions (PDF Helvetica-safe)."""
    if n is None:
        return ""
    # Whole number
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    # Mixed number with common fractions — use ASCII "1/3" not "⅓"
    # so reportlab/Helvetica never draws a missing-glyph black box.
    whole = int(n)
    frac = abs(n - whole)
    fractions = {
        0.125: "1/8",
        0.25: "1/4",
        0.333: "1/3",
        0.375: "3/8",
        0.5: "1/2",
        0.625: "5/8",
        0.666: "2/3",
        0.667: "2/3",
        0.75: "3/4",
        0.875: "7/8",
    }
    for key, glyph in fractions.items():
        if abs(frac - key) < 0.02:
            if whole == 0:
                return glyph
            return f"{whole} {glyph}"
    # Fallback: trim trailing zeros
    s = f"{n:.3f}".rstrip("0").rstrip(".")
    return s


@dataclass
class Ingredient:
    """One ingredient row in the TRN left column."""

    name: str
    quantity: Quantity
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    is_protein: bool = False
    group: str = ""  # optional visual grouping label

    def display_left(self, factor: float = 1.0) -> str:
        """Left-column label: '½ cup unsalted butter, softened'."""
        qty = self.quantity.display(factor)
        name = self.name
        if self.quantity.note:
            name = f"{name}, {self.quantity.note}"
        if qty:
            return f"{qty}  {name}"
        return name

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "quantity": self.quantity.to_dict(),
            "is_protein": self.is_protein,
            "group": self.group,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Ingredient":
        return cls(
            id=d.get("id", str(uuid.uuid4())[:8]),
            name=d["name"],
            quantity=Quantity.from_dict(d.get("quantity") or {}),
            is_protein=bool(d.get("is_protein", False)),
            group=d.get("group", ""),
        )


@dataclass
class Step:
    """One chronological action column in the TRN table."""

    action: str  # short verb phrase shown in the column header / cell
    detail: str = ""  # longer instruction text
    ingredient_ids: list[str] = field(default_factory=list)
    # Which prior step outputs this step consumes (for merges)
    consumes_steps: list[int] = field(default_factory=list)
    is_header: bool = False  # True for top-of-table prep (preheat, grease pan)
    duration: str = ""  # optional "10 min", "until golden"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(
            action=d.get("action", ""),
            detail=d.get("detail", ""),
            ingredient_ids=list(d.get("ingredient_ids") or []),
            consumes_steps=list(d.get("consumes_steps") or []),
            is_header=bool(d.get("is_header", False)),
            duration=d.get("duration", ""),
        )


@dataclass
class TRNTable:
    """
    Full Tabular Recipe Notation representation.

    Layout mental model (matches Cooking for Engineers):
      ┌─────────────┬────────┬────────┬────────┐
      │             │ preheat│ grease │        │  ← header steps (whole-dish)
      ├─────────────┼────────┼────────┼────────┤
      │ 2 eggs      │ beat   │        │        │
      │ 1 cup sugar │        │ cream  │        │  ← ingredient rows
      │ ½ cup butter│        │        │        │
      │ flour…      │        │        │ fold   │
      └─────────────┴────────┴────────┴────────┘
    Cells "merge" conceptually: a step that uses multiple ingredients spans
    those rows; combining prior mixtures spans those columns.
    """

    title: str
    ingredients: list[Ingredient] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    servings: Optional[str] = None
    source: str = ""  # url or "pasted text" / "upload"
    notes: str = ""
    scale_factor: float = 1.0
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    original_protein: Optional[str] = None
    substituted_protein: Optional[str] = None

    # ── serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "ingredients": [i.to_dict() for i in self.ingredients],
            "steps": [s.to_dict() for s in self.steps],
            "servings": self.servings,
            "source": self.source,
            "notes": self.notes,
            "scale_factor": self.scale_factor,
            "created_at": self.created_at,
            "original_protein": self.original_protein,
            "substituted_protein": self.substituted_protein,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "TRNTable":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            title=d.get("title", "Untitled Recipe"),
            ingredients=[Ingredient.from_dict(i) for i in d.get("ingredients") or []],
            steps=[Step.from_dict(s) for s in d.get("steps") or []],
            servings=d.get("servings"),
            source=d.get("source", ""),
            notes=d.get("notes", ""),
            scale_factor=float(d.get("scale_factor", 1.0)),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            original_protein=d.get("original_protein"),
            substituted_protein=d.get("substituted_protein"),
        )

    @classmethod
    def from_json(cls, text: str) -> "TRNTable":
        return cls.from_dict(json.loads(text))

    def header_steps(self) -> list[Step]:
        return [s for s in self.steps if s.is_header]

    def body_steps(self) -> list[Step]:
        return [s for s in self.steps if not s.is_header]

    def ingredient_index(self) -> dict[str, int]:
        return {ing.id: i for i, ing in enumerate(self.ingredients)}
