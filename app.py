#!/usr/bin/env python3
"""
Recipe Tables — local Streamlit app
====================================
Convert any recipe into Michael Chu's Tabular Recipe Notation (TRN)
and export a clean, shareable PDF.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import os
import re
import sys
from copy import deepcopy
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

# Load .env before any llm_available() checks
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Ensure project root is on path when launched as `streamlit run app.py`
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.models import TRNTable, Ingredient, Step, Quantity
from src.trn_converter import convert_text_to_trn, apply_scale
from src.trn_renderer import build_grid, grid_to_html, steps_detail_html, table_to_markdown
from src.pdf_exporter import export_pdf
from src.recipe_input import load_from_text, load_from_url, load_from_upload
from src.protein_sub import (
    list_proteins,
    add_protein,
    detect_proteins_in_table,
    substitute_protein,
)
from src.library import save_recipe, list_recipes, load_recipe, delete_recipe
from src.github_push import (
    GitHubConfig,
    load_settings,
    save_settings,
    push_recipe_folder,
    test_connection,
)
from src.examples import EXAMPLES, get_example, example_names
from src.quality import assess_conversion
from src.extract import extract_and_convert
from src.llm_extract import llm_available

# ─── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Recipe Tables",
    page_icon="📗",
    layout="wide",
    initial_sidebar_state="expanded",
)

SCALE_OPTIONS = {
    "½×": 0.5,
    "1×": 1.0,
    "1½×": 1.5,
    "2×": 2.0,
    "3×": 3.0,
}

EXPORTS_DIR = os.path.join(ROOT, "data", "exports")
os.makedirs(EXPORTS_DIR, exist_ok=True)


# ─── Session state ───────────────────────────────────────────────────────────
def _init_state() -> None:
    defaults = {
        "table": None,  # TRNTable | None
        "raw_text": "",
        "dark_mode": False,
        "enable_edit": False,
        "status_msg": "",
        "status_ok": True,
        "last_pdf": None,  # bytes
        "last_folder": None,
        "scale_label": "1×",
        "gh_push_on_save": True,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


# ─── Theme CSS ───────────────────────────────────────────────────────────────
def inject_theme(dark: bool) -> None:
    if dark:
        css = """
        <style>
        .stApp { background-color: #121412; color: #e8e8e8; }
        section[data-testid="stSidebar"] { background-color: #1a1e1a; }
        h1, h2, h3 { color: #7dcea0 !important; }
        .trn-banner {
            background: linear-gradient(135deg, #1a3d2b 0%, #0f2419 100%);
            border: 1px solid #2d6a4f; border-radius: 12px;
            padding: 1.1rem 1.4rem; margin-bottom: 1rem;
        }
        .trn-banner p { color: #b7d7c4; margin: 0.2rem 0 0 0; font-size: 0.95rem; }
        .trn-banner h1 { color: #d8f3dc !important; margin: 0; font-size: 1.7rem; }
        </style>
        """
    else:
        css = """
        <style>
        .stApp { background-color: #f7faf8; }
        h1, h2, h3 { color: #1b4332 !important; }
        .trn-banner {
            background: linear-gradient(135deg, #d8f3dc 0%, #f0fff4 100%);
            border: 1px solid #95d5b2; border-radius: 12px;
            padding: 1.1rem 1.4rem; margin-bottom: 1rem;
        }
        .trn-banner p { color: #2d6a4f; margin: 0.2rem 0 0 0; font-size: 0.95rem; }
        .trn-banner h1 { color: #1b4332 !important; margin: 0; font-size: 1.7rem; }
        div[data-testid="stToolbar"] { visibility: hidden; }
        </style>
        """
    st.markdown(css, unsafe_allow_html=True)


# ─── Sidebar ─────────────────────────────────────────────────────────────────
def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("### 📗 Recipe Tables")
        st.caption("Tabular Recipe Notation · local-first")

        st.session_state.dark_mode = st.toggle(
            "Dark mode", value=st.session_state.dark_mode
        )
        st.session_state.enable_edit = st.toggle(
            "Enable table editing",
            value=st.session_state.enable_edit,
            help="Show editable fields to fix OCR or conversion mistakes. "
            "Turn this on whenever a conversion looks wrong.",
        )
        st.markdown("#### Extract engine (ads)")
        if llm_available():
            st.success("**On** — SpaceXAI strips ads before TRN")
        else:
            st.warning(
                "**Off** — real blog/magazine pages will keep failing. "
                "Heuristics cannot navigate ads."
            )
        xai_in = st.text_input(
            "XAI_API_KEY (SpaceXAI)",
            value="",
            type="password",
            help="Get a key at https://console.x.ai — paste here (this session) or Save to .env",
            placeholder="xai-… (paste to enable extract)",
            key="xai_key_input",
        )
        if xai_in and xai_in.strip().startswith("xai-") and len(xai_in.strip()) > 12:
            os.environ["XAI_API_KEY"] = xai_in.strip()
        c_key1, c_key2 = st.columns(2)
        if c_key1.button("Use key this session", use_container_width=True, key="use_xai"):
            key = (xai_in or "").strip()
            if not key:
                st.error("Paste a key first.")
            else:
                os.environ["XAI_API_KEY"] = key
                st.success("Extract engine on for this session.")
                st.rerun()
        if c_key2.button("Save key to .env", use_container_width=True, key="save_xai"):
            key = (xai_in or os.environ.get("XAI_API_KEY") or "").strip()
            if not key:
                st.error("Paste a key first.")
            else:
                _persist_xai_key(key)
                os.environ["XAI_API_KEY"] = key
                st.success("Saved to .env — extract engine is on.")
                st.rerun()
        st.caption(
            "**Pipeline:** fetch page (JSON-LD → reader) → LLM clean recipe JSON "
            "→ TRN table. Ads never enter the table. Laptop today; same code can "
            "run as a cloud API later."
        )
        st.session_state.gh_push_on_save = st.toggle(
            "Push to GitHub on save",
            value=st.session_state.gh_push_on_save,
            help="When saving/generating a PDF, also push to your GitHub library repo.",
        )

        st.divider()
        st.markdown("#### Examples")
        for name in example_names():
            if st.button(name, key=f"ex_{name}", use_container_width=True):
                t = get_example(name)
                st.session_state.table = t
                st.session_state.raw_text = EXAMPLES[name]
                st.session_state.scale_label = "1×"
                st.session_state.last_pdf = None
                st.session_state.status_msg = f"Loaded example: {name}"
                st.session_state.status_ok = True
                st.rerun()

        st.divider()
        st.markdown("#### Local library")
        entries = list_recipes()
        if not entries:
            st.caption("No saved recipes yet.")
        else:
            for e in entries[:20]:
                cols = st.columns([4, 1])
                label = e.get("title") or e.get("folder")
                date = (e.get("saved_at") or "")[:10]
                if cols[0].button(
                    f"📄 {label}",
                    key=f"lib_{e.get('folder')}",
                    use_container_width=True,
                    help=date,
                ):
                    try:
                        st.session_state.table = load_recipe(e["path"])
                        st.session_state.last_folder = e["path"]
                        st.session_state.scale_label = "1×"
                        st.session_state.last_pdf = None
                        st.session_state.status_msg = f"Loaded from library: {label}"
                        st.session_state.status_ok = True
                        st.rerun()
                    except Exception as ex:
                        st.error(str(ex))
                if cols[1].button("🗑", key=f"del_{e.get('folder')}", help="Delete"):
                    delete_recipe(e["path"])
                    st.rerun()

        st.divider()
        with st.expander("⚙ GitHub settings", expanded=False):
            cfg = load_settings()
            token = st.text_input(
                "Personal access token",
                value=cfg.token if cfg.token and not cfg.token.startswith("ghp_your") else "",
                type="password",
                help="GitHub PAT with repo scope. Prefer setting GITHUB_TOKEN in .env.",
                placeholder="ghp_…",
            )
            repo = st.text_input(
                "Repository (owner/repo)",
                value="" if "your-username" in (cfg.repo or "") else cfg.repo,
                placeholder="your-username/recipe-tables-library",
                help="Replace with YOUR GitHub username and repo name.",
            )
            branch = st.text_input("Branch", value=cfg.branch or "main")
            prefix = st.text_input("Path prefix", value=cfg.path_prefix or "recipes/")
            persist = st.checkbox("Save token to local settings file", value=False)

            c1, c2 = st.columns(2)
            if c1.button("Save settings", use_container_width=True):
                new_cfg = GitHubConfig(
                    token=token or cfg.token,
                    repo=repo or cfg.repo,
                    branch=branch or "main",
                    path_prefix=prefix or "recipes/",
                )
                save_settings(new_cfg, persist_token=persist)
                # Also set env for this process
                if new_cfg.token:
                    os.environ["GITHUB_TOKEN"] = new_cfg.token
                if new_cfg.repo:
                    os.environ["GITHUB_REPO"] = new_cfg.repo
                os.environ["GITHUB_BRANCH"] = new_cfg.branch
                os.environ["GITHUB_PATH_PREFIX"] = new_cfg.path_prefix
                st.success("Settings saved.")
            if c2.button("Test connection", use_container_width=True):
                test_cfg = GitHubConfig(
                    token=token or cfg.token,
                    repo=repo or cfg.repo,
                    branch=branch or "main",
                    path_prefix=prefix or "recipes/",
                )
                result = test_connection(test_cfg)
                if result.ok:
                    st.success(result.message)
                else:
                    st.error(result.message)

            st.caption(
                "Create a fine-grained or classic PAT at github.com/settings/tokens. "
                "Copy `.env.example` → `.env` and fill in your values for a durable setup."
            )

        with st.expander("🥩 Protein list", expanded=False):
            prots = list_proteins()
            st.write(", ".join(prots))
            new_p = st.text_input("Add protein", placeholder="e.g. duck")
            if st.button("Add to list") and new_p.strip():
                add_protein(new_p.strip())
                st.success(f"Added “{new_p.strip()}”")
                st.rerun()
            st.caption("Or edit `data/proteins.json` directly.")


# ─── Input panel ─────────────────────────────────────────────────────────────
def render_input() -> None:
    st.markdown("### 1 · Import a recipe")
    tab_text, tab_url, tab_file = st.tabs(["Paste text", "From URL", "Upload PDF / image"])

    with tab_text:
        text = st.text_area(
            "Recipe text",
            value=st.session_state.raw_text,
            height=220,
            placeholder=(
                "Paste a full recipe here…\n\n"
                "Title\n\nIngredients\n- 1 cup flour\n…\n\n"
                "Instructions\n1. Preheat oven…\n…"
            ),
            label_visibility="collapsed",
        )
        if st.button("Convert text → TRN", type="primary", key="btn_text"):
            if not text.strip():
                st.warning("Paste a recipe first.")
            else:
                raw, source, title_hint, method = load_from_text(text)
                _convert_and_store(raw, source, title_hint=title_hint, fetch_method=method)

    with tab_url:
        url = st.text_input("Recipe URL", placeholder="https://…")
        if st.button("Fetch & convert", type="primary", key="btn_url"):
            if not url.strip():
                st.warning("Enter a URL.")
            else:
                with st.spinner(
                    "Fetching recipe (JSON-LD → browser scrape → reader fallback)…"
                ):
                    try:
                        raw, source, title_hint, method = load_from_url(url.strip())
                        st.session_state.raw_text = raw
                        _convert_and_store(
                            raw, source, title_hint=title_hint, fetch_method=method
                        )
                    except Exception as e:
                        msg = str(e)
                        st.error(msg)
                        st.info(
                            "If the site blocks fetch: **Upload PDF** (Print → Save as PDF) "
                            "or **Paste text** (ingredients + instructions only). "
                            "With XAI_API_KEY on, pages that *do* load get ads stripped by LLM."
                        )

    with tab_file:
        up = st.file_uploader(
            "PDF or image (OCR)",
            type=["pdf", "png", "jpg", "jpeg", "webp", "tif", "tiff", "txt", "md"],
        )
        if st.button("Extract & convert", type="primary", key="btn_file"):
            if up is None:
                st.warning("Choose a file first.")
            else:
                with st.spinner("Running OCR / text extraction…"):
                    raw, source, title_hint, method = load_from_upload(
                        up.getvalue(), up.name
                    )
                    st.session_state.raw_text = raw
                    if raw.startswith("[OCR") or raw.startswith("[Image OCR"):
                        st.warning(raw)
                    _convert_and_store(
                        raw, source, title_hint=title_hint, fetch_method=method
                    )


def _persist_xai_key(key: str) -> None:
    """Write or update XAI_API_KEY in project .env (gitignored)."""
    env_path = os.path.join(ROOT, ".env")
    lines: list[str] = []
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    out: list[str] = []
    found = False
    for ln in lines:
        if re.match(r"^\s*XAI_API_KEY\s*=", ln):
            out.append(f"XAI_API_KEY={key}\n")
            found = True
        else:
            out.append(ln)
    if not found:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"\n# LLM extract (SpaceXAI / xAI)\nXAI_API_KEY={key}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(out)


def _convert_and_store(
    raw: str, source: str, title_hint=None, fetch_method: str = ""
) -> None:
    """
    Engineering pipeline: messy text → structured draft → TRN.
    Ads never enter TRN; quality gate fails closed into edit mode.
    """
    table, draft, meta = extract_and_convert(
        raw,
        source=source,
        title_hint=title_hint,
        prefer_llm=True,
        fetch_method=fetch_method or "",
    )

    report = assess_conversion(table)
    st.session_state.table = table
    st.session_state.raw_text = raw
    st.session_state.scale_label = "1×"
    st.session_state.last_pdf = None
    st.session_state.quality = report
    st.session_state.extract_meta = meta

    extractor = meta.get("extractor") or "?"
    method = meta.get("fetch_method") or fetch_method or ""
    parts = []
    if method:
        parts.append(f"fetch={method}")
    parts.append(f"extract={extractor}")
    if extractor == "llm":
        parts.append("ads stripped upstream")
    elif extractor == "jsonld":
        parts.append("schema.org (no ads in data)")
    elif meta.get("llm_error"):
        parts.append(f"LLM: {str(meta['llm_error'])[:70]}")
    elif not llm_available() and meta.get("messy"):
        parts.append("set XAI_API_KEY for ad pages")
    llm_note = " · " + " · ".join(parts)

    if report.ok:
        st.session_state.status_msg = (
            f"Converted “{table.title}” "
            f"({len(table.ingredients)} ingredients, {len(table.steps)} steps) "
            f"— quality {report.score}/100{llm_note}"
        )
        st.session_state.status_ok = True
    else:
        st.session_state.enable_edit = True
        hint = meta.get("hint") or ""
        st.session_state.status_msg = report.summary() + llm_note + (
            f"\n\n{hint}" if hint else ""
        )
        st.session_state.status_ok = False
    st.rerun()


# ─── Edit panel ──────────────────────────────────────────────────────────────
def render_editor(table: TRNTable) -> TRNTable:
    """Simple editable preview — fix OCR/conversion mistakes."""
    st.markdown("### Edit table")
    table = deepcopy(table)

    table.title = st.text_input("Title", value=table.title)
    table.servings = st.text_input("Servings", value=table.servings or "")
    table.notes = st.text_area("Notes", value=table.notes or "", height=60)

    st.markdown("#### Ingredients")
    remove_ing = []
    for i, ing in enumerate(table.ingredients):
        c1, c2, c3, c4, c5 = st.columns([2, 2, 3, 1, 1])
        amt = c1.text_input(
            "Amt",
            value="" if ing.quantity.amount is None else str(ing.quantity.amount),
            key=f"ing_amt_{ing.id}",
            label_visibility="collapsed",
            placeholder="amount",
        )
        unit = c2.text_input(
            "Unit",
            value=ing.quantity.unit,
            key=f"ing_unit_{ing.id}",
            label_visibility="collapsed",
            placeholder="unit",
        )
        name = c3.text_input(
            "Name",
            value=ing.name,
            key=f"ing_name_{ing.id}",
            label_visibility="collapsed",
            placeholder="ingredient",
        )
        ing.is_protein = c4.checkbox(
            "P",
            value=ing.is_protein,
            key=f"ing_prot_{ing.id}",
            help="Mark as protein",
        )
        if c5.button("✕", key=f"ing_del_{ing.id}"):
            remove_ing.append(i)
        try:
            ing.quantity.amount = float(amt) if amt.strip() else None
        except ValueError:
            ing.quantity.raw = amt
            ing.quantity.amount = None
        ing.quantity.unit = unit
        ing.name = name

    table.ingredients = [
        ing for i, ing in enumerate(table.ingredients) if i not in remove_ing
    ]
    if st.button("＋ Add ingredient"):
        table.ingredients.append(
            Ingredient(name="new ingredient", quantity=Quantity(amount=1, unit=""))
        )
        st.session_state.table = table
        st.rerun()

    st.markdown("#### Steps")
    remove_steps = []
    for i, step in enumerate(table.steps):
        c1, c2, c3 = st.columns([2, 5, 1])
        step.action = c1.text_input(
            "Action",
            value=step.action,
            key=f"st_act_{i}",
            label_visibility="collapsed",
            placeholder="action",
        )
        step.detail = c2.text_input(
            "Detail",
            value=step.detail,
            key=f"st_det_{i}",
            label_visibility="collapsed",
            placeholder="instruction",
        )
        step.is_header = st.checkbox(
            "Header / prep step",
            value=step.is_header,
            key=f"st_hdr_{i}",
        )
        if c3.button("✕", key=f"st_del_{i}"):
            remove_steps.append(i)
    table.steps = [s for i, s in enumerate(table.steps) if i not in remove_steps]
    if st.button("＋ Add step"):
        table.steps.append(Step(action="mix", detail="Describe the step…"))
        st.session_state.table = table
        st.rerun()

    # Re-link ingredient ids after edits (names may have changed — keep ids)
    return table


# ─── Controls: scale + protein ───────────────────────────────────────────────
def render_controls(table: TRNTable) -> TRNTable:
    st.markdown("### 2 · Adjust")
    c1, c2, c3 = st.columns([2, 3, 2])

    with c1:
        labels = list(SCALE_OPTIONS.keys())
        current = st.session_state.scale_label
        if current not in labels:
            current = "1×"
        choice = st.selectbox("Scale", labels, index=labels.index(current))
        if choice != st.session_state.scale_label:
            factor = SCALE_OPTIONS[choice]
            table = apply_scale(table, factor)
            st.session_state.scale_label = choice
            st.session_state.table = table
            st.session_state.last_pdf = None
            st.rerun()

    with c2:
        detected = detect_proteins_in_table(table)
        prots = list_proteins()
        st.caption(
            "Detected protein: "
            + (", ".join(detected) if detected else "none — mark one in the editor or pick a substitute anyway")
        )
        sub = st.selectbox(
            "Substitute protein",
            options=["— keep original —"] + prots,
            help="Replaces the main protein in ingredients and step text.",
        )
        if st.button("Apply protein substitution", use_container_width=True):
            if sub == "— keep original —":
                st.info("Choose a protein first.")
            else:
                original = detected[0] if detected else table.original_protein
                table = substitute_protein(table, sub, original=original)
                st.session_state.table = table
                st.session_state.last_pdf = None
                st.session_state.status_msg = (
                    f"Substituted protein → {sub}"
                    + (f" (was {original})" if original else "")
                )
                st.session_state.status_ok = True
                st.rerun()

    with c3:
        st.caption("Quick scales")
        cols = st.columns(len(SCALE_OPTIONS))
        for col, (lab, fac) in zip(cols, SCALE_OPTIONS.items()):
            if col.button(lab, key=f"qs_{lab}", use_container_width=True):
                table = apply_scale(table, fac)
                st.session_state.scale_label = lab
                st.session_state.table = table
                st.session_state.last_pdf = None
                st.rerun()

    return table


# ─── Preview + export ────────────────────────────────────────────────────────
def render_preview_and_export(table: TRNTable) -> None:
    st.markdown("### 3 · TRN preview")

    report = st.session_state.get("quality") or assess_conversion(table)
    if report.ok:
        st.success(f"Quality check: {report.score}/100 — looks usable")
    else:
        st.error(report.summary())
        st.warning(
            "**This is not a joke and not “almost fine.”** "
            "Blog/magazine PDFs are messy. Best path:\n"
            "1. Turn on **Enable table editing** (sidebar) and fix title/ingredients/steps, or\n"
            "2. Paste **only** the ingredient list + numbered instructions (no nutrition, ads, “more recipes”), or\n"
            "3. Use a simple example from the sidebar to verify the app itself works."
        )

    dark = st.session_state.dark_mode
    grid = build_grid(table)
    html = grid_to_html(grid, dark=dark)
    detail = steps_detail_html(table, dark=dark)
    st.markdown(html + detail, unsafe_allow_html=True)

    st.markdown("### 4 · Export")
    c1, c2, c3, c4 = st.columns(4)

    with c1:
        export_label = "Generate PDF" if report.ok else "Generate PDF anyway"
        if st.button(export_label, type="primary", use_container_width=True):
            if not report.ok:
                st.session_state.status_msg = (
                    "PDF generated despite FAILED quality check — review carefully."
                )
                st.session_state.status_ok = False
            with st.spinner("Rendering PDF…"):
                pdf = export_pdf(table)
                st.session_state.last_pdf = pdf
                # Auto-save to library
                folder = save_recipe(table, pdf_bytes=pdf)
                st.session_state.last_folder = folder
                st.session_state.status_msg = (
                    (st.session_state.status_msg + " · " if not report.ok else "")
                    + f"PDF ready · saved to library ({os.path.basename(folder)})"
                )
                if report.ok:
                    st.session_state.status_ok = True
                if st.session_state.gh_push_on_save:
                    result = push_recipe_folder(folder)
                    if result.ok:
                        st.session_state.status_msg += f" · GitHub: {result.message}"
                    else:
                        st.session_state.status_msg += f" · GitHub skipped: {result.message}"
                        st.session_state.status_ok = False
            st.rerun()

    with c2:
        if st.button("Save to library only", use_container_width=True):
            folder = save_recipe(table, pdf_bytes=st.session_state.last_pdf)
            st.session_state.last_folder = folder
            msg = f"Saved to library ({os.path.basename(folder)})"
            ok = True
            if st.session_state.gh_push_on_save:
                result = push_recipe_folder(folder)
                if result.ok:
                    msg += f" · GitHub: {result.message}"
                else:
                    msg += f" · GitHub: {result.message}"
                    ok = False
            st.session_state.status_msg = msg
            st.session_state.status_ok = ok
            st.rerun()

    with c3:
        if st.button("Push to GitHub now", use_container_width=True):
            folder = st.session_state.last_folder
            if not folder or not os.path.isdir(folder):
                # Save first
                pdf = st.session_state.last_pdf or export_pdf(table)
                st.session_state.last_pdf = pdf
                folder = save_recipe(table, pdf_bytes=pdf)
                st.session_state.last_folder = folder
            result = push_recipe_folder(folder)
            st.session_state.status_msg = result.message
            st.session_state.status_ok = result.ok
            if result.urls:
                st.session_state.status_msg += " · " + result.urls[0]
            st.rerun()

    with c4:
        md = table_to_markdown(table)
        st.download_button(
            "Download Markdown",
            data=md.encode("utf-8"),
            file_name=f"{_safe_filename(table.title)}.md",
            mime="text/markdown",
            use_container_width=True,
        )

    if st.session_state.last_pdf:
        st.download_button(
            "⬇ Download PDF",
            data=st.session_state.last_pdf,
            file_name=f"{_safe_filename(table.title)}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
        # Also write to exports for convenience
        out = os.path.join(EXPORTS_DIR, f"{_safe_filename(table.title)}.pdf")
        try:
            with open(out, "wb") as f:
                f.write(st.session_state.last_pdf)
            st.caption(f"Also written to `{out}`")
        except Exception:
            pass


def _safe_filename(title: str) -> str:
    import re

    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", title).strip("_")
    return (s[:60] or "recipe")


# ─── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    inject_theme(st.session_state.dark_mode)
    render_sidebar()

    st.markdown(
        """
        <div class="trn-banner">
          <h1>Recipe Tables</h1>
          <p>Convert any recipe into Michael Chu’s Tabular Recipe Notation (Cooking for Engineers, 2004) and export a clean PDF.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.session_state.status_msg:
        if st.session_state.status_ok:
            st.success(st.session_state.status_msg)
        else:
            st.warning(st.session_state.status_msg)

    render_input()
    st.divider()

    table: TRNTable | None = st.session_state.table
    if table is None:
        st.info(
            "Import a recipe above, or load an **example** from the sidebar "
            "(try **Classic Fudge Brownies**)."
        )
        return

    if st.session_state.enable_edit:
        table = render_editor(table)
        if st.button("Apply edits"):
            st.session_state.table = table
            st.session_state.last_pdf = None
            st.success("Edits applied.")
            st.rerun()
        st.divider()

    table = render_controls(table)
    # Keep session in sync after controls that don't rerun
    st.session_state.table = table
    st.divider()
    render_preview_and_export(table)


if __name__ == "__main__":
    main()
