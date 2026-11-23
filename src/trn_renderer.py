"""
HTML / grid renderer for TRN — driven entirely by trn_engine.

All recipe-type logic lives in trn_engine.build_trn(). This module only
paints the EngineResult into HTML cells / markdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import TRNTable
from .trn_engine import build_trn, action_label, EngineResult

CFE_GREEN = "#22a05a"


@dataclass
class GridCell:
    text: str = ""
    rowspan: int = 1
    colspan: int = 1
    kind: str = "empty"  # ingredient | action | blank | prep
    skipped: bool = False
    vertical: bool = False


@dataclass
class TRNGrid:
    title: str
    n_cols: int
    rows: list[list[GridCell]]
    n_prep_rows: int = 0
    scale_factor: float = 1.0
    servings: Optional[str] = None
    notes: str = ""
    action_labels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_grid(table: TRNTable) -> TRNGrid:
    """
    Build a CFE-style grid for any recipe via the universal TRN engine.
    """
    eng = build_trn(table)
    return engine_to_grid(eng)


def engine_to_grid(eng: EngineResult) -> TRNGrid:
    ingredients = eng.ingredients
    milestones = eng.milestones
    spans = eng.spans
    factor = eng.scale_factor or 1.0

    n_ing = max(len(ingredients), 1)
    n_action = max(len(milestones), 1)
    n_cols = 1 + n_action

    # ── action occupancy ─────────────────────────────────────────────────
    action_master: list[list[Optional[GridCell]]] = [
        [None for _ in range(n_action)] for _ in range(n_ing)
    ]
    covered = [[False] * n_action for _ in range(n_ing)]

    for col, m in enumerate(milestones):
        rows_here = sorted(spans[col]) if col < len(spans) else [0]
        if not rows_here:
            rows_here = [0]
        label = action_label(m)
        # solid blocks only
        blocks = _consecutive_blocks(rows_here)
        for start, length in blocks:
            while start < n_ing and covered[start][col]:
                start += 1
                length -= 1
            if length <= 0 or start >= n_ing:
                continue
            length = min(length, n_ing - start)
            action_master[start][col] = GridCell(
                text=label,
                rowspan=length,
                kind="action",
                vertical=(length >= 2),
            )
            for k in range(length):
                covered[start + k][col] = True

    rows_out: list[list[GridCell]] = []

    # Prep rows — full width inside the table
    for p in eng.prep:
        text = (p.detail or p.action or "").strip().rstrip(".")
        if len(text) > 100:
            text = text[:97] + "…"
        row = [GridCell(text=text, colspan=n_cols, kind="prep")]
        for _ in range(n_cols - 1):
            row.append(GridCell(skipped=True, kind="prep"))
        rows_out.append(row)

    # Ingredient rows
    for r in range(n_ing):
        row: list[GridCell] = []
        if ingredients:
            row.append(
                GridCell(
                    text=ingredients[r].display_left(factor),
                    kind="ingredient",
                )
            )
        else:
            row.append(GridCell(text="(no ingredients)", kind="ingredient"))

        c = 0
        while c < n_action:
            if covered[r][c] and action_master[r][c] is not None:
                row.append(action_master[r][c])  # type: ignore
                c += 1
            elif covered[r][c]:
                row.append(GridCell(skipped=True, kind="action"))
                c += 1
            else:
                span = 1
                while (
                    c + span < n_action
                    and not covered[r][c + span]
                    and action_master[r][c + span] is None
                ):
                    span += 1
                row.append(GridCell(text="", kind="blank", colspan=span))
                for _ in range(span - 1):
                    row.append(GridCell(skipped=True, kind="blank"))
                c += span
        rows_out.append(row)

    labels = [action_label(m) for m in milestones]
    return TRNGrid(
        title=eng.title,
        n_cols=n_cols,
        rows=rows_out,
        n_prep_rows=len(eng.prep),
        scale_factor=factor,
        servings=eng.servings,
        notes=eng.notes,
        action_labels=labels,
        warnings=list(eng.warnings),
    )


def _consecutive_blocks(indices: list[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    indices = sorted(set(indices))
    blocks = []
    start = indices[0]
    length = 1
    for x in indices[1:]:
        if x == start + length:
            length += 1
        else:
            blocks.append((start, length))
            start = x
            length = 1
    blocks.append((start, length))
    return blocks


# ── HTML ─────────────────────────────────────────────────────────────────────

def grid_to_html(grid: TRNGrid, dark: bool = False) -> str:
    if dark:
        border = "#3dba6e"
        bg = "#141814"
        text = "#e8f0ea"
        title_c = "#8fd4a8"
        muted = "#8fb89a"
    else:
        border = CFE_GREEN
        bg = "#ffffff"
        text = "#111111"
        title_c = "#0d3d24"
        muted = "#3d5c4a"

    parts = [
        f'<div class="trn-wrap" style="font-family: Georgia, \'Palatino Linotype\', '
        f'Times, serif; color:{text}; max-width:100%; overflow-x:auto;">',
        f'<h2 style="margin:0 0 0.5rem 0; color:{title_c}; font-weight:700; '
        f'font-size:1.4rem;">{_esc(grid.title)}</h2>',
    ]
    meta = []
    if grid.servings:
        meta.append(f"Serves {_esc(str(grid.servings))}")
    if grid.scale_factor and abs(grid.scale_factor - 1.0) > 1e-6:
        meta.append(f"Scale {_format_scale(grid.scale_factor)}")
    if meta:
        parts.append(
            f'<p style="margin:0 0 0.55rem 0; color:{muted}; font-size:0.88rem;">'
            + " · ".join(meta)
            + "</p>"
        )

    parts.append(
        f"""<style>
        table.TRN {{
            border-collapse: collapse;
            border: 3px solid {border};
            width: auto;
            max-width: 100%;
            background: {bg};
            font-size: 0.95rem;
            line-height: 1.3;
        }}
        table.TRN td {{
            border: 1.5px solid {border};
            padding: 0.4rem 0.65rem;
            vertical-align: middle;
            background: {bg};
            color: {text};
        }}
        table.TRN td.ingredient {{
            text-align: left;
            font-weight: 500;
            min-width: 12rem;
            max-width: 22rem;
        }}
        table.TRN td.action {{
            text-align: center;
            font-weight: 600;
            min-width: 2.4rem;
            max-width: 5.5rem;
            padding: 0.5rem 0.4rem;
        }}
        table.TRN td.action.vertical {{
            writing-mode: vertical-rl;
            transform: rotate(180deg);
            text-align: center;
            letter-spacing: 0.06em;
            padding: 0.75rem 0.35rem;
            min-width: 2rem;
            max-width: 3.2rem;
            font-size: 0.9rem;
        }}
        table.TRN td.blank {{ min-width: 1.2rem; }}
        table.TRN td.prep {{
            text-align: center;
            font-weight: 500;
            font-size: 0.98rem;
            padding: 0.5rem 0.75rem;
        }}
        </style>"""
    )

    parts.append('<table class="TRN">')
    for row in grid.rows:
        parts.append("<tr>")
        for cell in row:
            if cell.skipped:
                continue
            cls = cell.kind
            if cell.kind == "action" and cell.vertical:
                cls = "action vertical"
            rs = f' rowspan="{cell.rowspan}"' if cell.rowspan > 1 else ""
            cs = f' colspan="{cell.colspan}"' if cell.colspan > 1 else ""
            if cell.vertical:
                content = _esc(cell.text.replace("\n", " "))
            else:
                content = _esc(cell.text).replace("\n", "<br/>")
            parts.append(f'<td class="{cls}"{rs}{cs}>{content}</td>')
        parts.append("</tr>")
    parts.append("</table>")

    if grid.notes:
        parts.append(
            f'<p style="margin-top:0.65rem; color:{muted}; font-size:0.85rem;">'
            f"<em>{_esc(grid.notes)}</em></p>"
        )
    parts.append(
        f'<p style="margin-top:0.45rem; color:{muted}; font-size:0.72rem; '
        f'font-style:italic;">Tabular Recipe Notation · Michael Chu, '
        f"Cooking for Engineers (2004)</p>"
    )
    parts.append("</div>")
    return "\n".join(parts)


def steps_detail_html(table: TRNTable, dark: bool = False) -> str:
    muted = "#8fb89a" if dark else "#3d5c4a"
    text = "#e6eee8" if dark else "#1a1a1a"
    # Always use original method steps from engine (unmutated)
    eng = build_trn(table)
    steps = eng.method_steps or table.steps
    if not steps:
        return ""
    items = []
    for i, s in enumerate(steps, 1):
        tag = "Prep" if s.is_header else str(i)
        items.append(
            f'<li style="margin-bottom:0.3rem;"><span style="color:{muted}; '
            f'font-weight:600;">{tag}.</span> {_esc(s.detail or s.action)}</li>'
        )
    return (
        f'<div style="margin-top:1.1rem; color:{text}; font-family: system-ui, sans-serif; '
        f'font-size:0.88rem;">'
        f'<p style="color:{muted}; margin:0 0 0.35rem 0; font-weight:600; '
        f'font-family: Georgia, serif;">Method</p>'
        f'<ol style="padding-left:1.15rem; margin:0;">{"".join(items)}</ol></div>'
    )


def table_to_markdown(table: TRNTable) -> str:
    eng = build_trn(table)
    grid = engine_to_grid(eng)
    lines = [f"# {eng.title}", ""]
    if eng.servings:
        lines.append(f"**Servings:** {eng.servings}  ")
    if abs((eng.scale_factor or 1.0) - 1.0) > 1e-6:
        lines.append(f"**Scale:** {_format_scale(eng.scale_factor)}  ")
    for p in eng.prep:
        lines.append(f"**Prep:** {p.detail or p.action}  ")
    lines.append("")
    lines.append("## Recipe summary table (TRN)")
    lines.append("")
    lines.append("```")
    factor = eng.scale_factor or 1.0
    act_labels = [action_label(m).replace("\n", " ") for m in eng.milestones]
    ing_w = max((len(i.display_left(factor)) for i in eng.ingredients), default=12)
    ing_w = min(max(ing_w, 14), 40)
    n_ing = len(eng.ingredients)
    n_steps = len(eng.milestones)
    matrix = [["" for _ in range(n_steps)] for _ in range(n_ing)]
    for r_idx, row in enumerate(grid.rows[grid.n_prep_rows :]):
        col = 0
        for cell in row:
            if cell.skipped:
                continue
            if cell.kind == "ingredient":
                col = 1
                continue
            if cell.kind == "action" and cell.text and col >= 1:
                si = col - 1
                if si < n_steps and r_idx < n_ing:
                    matrix[r_idx][si] = cell.text.replace("\n", " ")
            col += max(cell.colspan, 1)
    col_w = [max(len(a), 5) for a in act_labels] or [5]

    def pad(s: str, w: int) -> str:
        return s.ljust(w)[:w]

    header = pad("Ingredient", ing_w) + " │ " + " │ ".join(
        pad(a, col_w[i] if i < len(col_w) else 5) for i, a in enumerate(act_labels)
    )
    lines.append(header)
    lines.append("─" * min(len(header), 100))
    for r, ing in enumerate(eng.ingredients):
        cells = [pad(ing.display_left(factor), ing_w)]
        for si in range(n_steps):
            w = col_w[si] if si < len(col_w) else 5
            cells.append(pad(matrix[r][si] if r < len(matrix) else "", w))
        lines.append(" │ ".join(cells))
    lines.append("```")
    lines.append("")
    lines.append("## Method")
    lines.append("")
    for i, s in enumerate(eng.method_steps, 1):
        tag = "*(prep)* " if s.is_header else ""
        lines.append(f"{i}. {tag}{s.detail or s.action}")
    lines.append("")
    if eng.notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(eng.notes)
        lines.append("")
    if eng.source:
        lines.append(f"*Source: {eng.source}*")
    lines.append("")
    lines.append("---")
    lines.append(
        "*Tabular Recipe Notation inspired by Michael Chu, "
        "Cooking for Engineers (2004) · Generated by Recipe Tables*"
    )
    return "\n".join(lines)


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_scale(f: float) -> str:
    mapping = {0.5: "½×", 1.0: "1×", 1.5: "1½×", 2.0: "2×", 3.0: "3×"}
    for k, v in mapping.items():
        if abs(f - k) < 1e-6:
            return v
    return f"{f:g}×"
