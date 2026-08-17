"""
Local recipe library — stores structured JSON (+ optional PDF) on disk.

Each recipe is a folder:
  data/library/<slug>-<shortid>/
    recipe.json
    recipe.md
    recipe.pdf   (when generated)
    meta.json    (title, dates, source)
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Optional

from .models import TRNTable
from .trn_renderer import table_to_markdown

_DEFAULT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "library"
)


def _slug(title: str) -> str:
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")[:48] or "recipe"
    return s


def library_root(root: Optional[str] = None) -> str:
    r = root or _DEFAULT_ROOT
    os.makedirs(r, exist_ok=True)
    return r


def save_recipe(
    table: TRNTable,
    pdf_bytes: Optional[bytes] = None,
    root: Optional[str] = None,
) -> str:
    """
    Persist recipe to the local library. Returns the entry directory path.
    """
    root = library_root(root)
    folder_name = f"{_slug(table.title)}-{table.id[:8]}"
    path = os.path.join(root, folder_name)
    os.makedirs(path, exist_ok=True)

    with open(os.path.join(path, "recipe.json"), "w", encoding="utf-8") as f:
        f.write(table.to_json())

    with open(os.path.join(path, "recipe.md"), "w", encoding="utf-8") as f:
        f.write(table_to_markdown(table))

    if pdf_bytes:
        with open(os.path.join(path, "recipe.pdf"), "wb") as f:
            f.write(pdf_bytes)

    meta = {
        "id": table.id,
        "title": table.title,
        "created_at": table.created_at,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source": table.source,
        "scale_factor": table.scale_factor,
        "has_pdf": bool(pdf_bytes),
        "folder": folder_name,
    }
    with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")

    return path


def list_recipes(root: Optional[str] = None) -> list[dict]:
    root = library_root(root)
    entries = []
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        meta_path = os.path.join(folder, "meta.json")
        if not os.path.isdir(folder):
            continue
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta["path"] = folder
            entries.append(meta)
        elif os.path.isfile(os.path.join(folder, "recipe.json")):
            entries.append(
                {
                    "title": name,
                    "folder": name,
                    "path": folder,
                    "saved_at": "",
                    "has_pdf": os.path.isfile(os.path.join(folder, "recipe.pdf")),
                }
            )
    # Newest first
    entries.sort(key=lambda e: e.get("saved_at") or e.get("created_at") or "", reverse=True)
    return entries


def load_recipe(folder_path: str) -> TRNTable:
    with open(os.path.join(folder_path, "recipe.json"), "r", encoding="utf-8") as f:
        return TRNTable.from_json(f.read())


def delete_recipe(folder_path: str) -> None:
    if os.path.isdir(folder_path):
        # Safety: only delete under library root
        root = os.path.realpath(library_root())
        target = os.path.realpath(folder_path)
        if target.startswith(root + os.sep) or target == root:
            shutil.rmtree(target)
