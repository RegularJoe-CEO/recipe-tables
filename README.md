# Recipe Tables

**Convert any recipe into Michael Chu’s Tabular Recipe Notation (TRN)** — the classic green-bordered “recipe summary tables” from *[Cooking for Engineers](https://www.cookingforengineers.com/)* (2004) — and export a clean, shareable PDF.

Runs **on your machine** from this GitHub repo. No website login, no NDA. Optional LLM extract for ad-heavy pages.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-ff4b4b)
![License](https://img.shields.io/badge/license-MIT-green)

---

## From GitHub in one minute

```bash
git clone https://github.com/RegularJoe-CEO/recipe-tables.git
cd recipe-tables
make setup
make run
```

Open the URL Streamlit prints (usually **http://localhost:8501**).

Load **Classic Fudge Brownies** from the sidebar to see a full TRN table immediately.

### Docker (no local Python)

```bash
git clone https://github.com/RegularJoe-CEO/recipe-tables.git
cd recipe-tables
docker compose up --build
# → http://localhost:8501
```

Optional LLM for messy URLs:

```bash
export XAI_API_KEY=xai-...   # https://console.x.ai
docker compose up --build
```

---

## What it does

| Step | What it does |
|------|----------------|
| **Import** | Paste text, fetch a URL, or upload a PDF/image (OCR) |
| **TRN convert** | Left column = ingredients + measurements; right columns = chronological steps with merges; top header rows for preheat/grease/etc. |
| **Optional edit** | Toggle to fix OCR or conversion mistakes |
| **Scale** | ½× · 1× · 1½× · 2× · 3× — quantities update automatically |
| **Substitute protein** | Swap chicken → tofu (etc.) in ingredients **and** step text |
| **PDF export** | Title + classic green-bordered TRN table, print-ready |
| **Local library** | Saves structured JSON + Markdown + PDF under `data/library/` |
| **GitHub push** | Optionally commit each recipe into a library repo you control (e.g. [recipe-cards](https://github.com/RegularJoe-CEO/recipe-cards)) |

---

## Ad-heavy sites (required for real URLs)

Real recipe pages are full of ads, sign-in walls, and related posts. **Heuristics alone will keep failing** — that is normal, not a bug.

```
1. FETCH
   · Direct HTML with browser headers
   · Prefer schema.org Recipe JSON-LD
   · Else scrape recipe containers
   · If blocked (403) → Jina reader (r.jina.ai)
   · If still blocked → PDF upload or paste (always works)

2. EXTRACT  →  RecipeDraft (title, ingredients, steps only)
   · LLM via SpaceXAI (XAI_API_KEY) on messy pages
   · JSON-LD / structured path when fetch already clean
   · Heuristic fallback for clean paste only

3. TRN
   · Cascade table + PDF from RecipeDraft only
   · Ads never enter table logic
```

### Turn on the extract engine

1. Get a key at https://console.x.ai  
2. Either paste it in the app sidebar (**Extract engine**), or:

   ```bash
   cp .env.example .env
   # edit: XAI_API_KEY=xai-...
   ```

3. Convert again — status should show `extract=llm` or `extract=jsonld`

Without `XAI_API_KEY`, the app still runs offline for clean paste / simple PDFs.

---

## Prerequisites (local `make setup`)

- **Python 3.10+**
- **Tesseract OCR** (only for image/scanned-PDF OCR)
  - macOS: `brew install tesseract poppler`
  - Ubuntu/Debian: `sudo apt install tesseract-ocr poppler-utils`
  - Windows: [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) + [poppler](https://github.com/oschwartz10612/poppler-windows)

Docker image already includes Tesseract + poppler.

---

## Manual install (no Make)

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # optional
streamlit run app.py
```

---

## How to use

1. **Import** a recipe (text / URL / file) or pick an example.
2. Review the **TRN preview** (green-bordered table).
3. Optionally turn on **Enable table editing** in the sidebar and fix mistakes.
4. Use **Scale** and **Substitute protein** as needed.
5. Click **Generate PDF** — downloads + saves to the local library.
6. (Optional) Enable **Push to GitHub on save** once settings are configured.

---

## GitHub library push (optional)

Every save/PDF can push:

```
recipes/<slug-id>/
  recipe.md
  recipe.json
  recipe.pdf
```

```bash
cp .env.example .env
# GITHUB_TOKEN=ghp_...
# GITHUB_REPO=your-username/recipe-cards
# GITHUB_BRANCH=main
# GITHUB_PATH_PREFIX=recipes/
```

Or configure in the sidebar (**GitHub settings** → Test connection).

---

## Project layout

```
recipe-tables/
├── app.py                 # Streamlit UI
├── Makefile               # make setup / make run / make test
├── Dockerfile             # container run
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── src/                   # extract → TRN → PDF
├── data/
│   ├── proteins.json
│   ├── library/           # your saves (gitignored)
│   └── exports/
└── tests/
```

---

## Tests

```bash
make test
# or: pytest tests/ -q
```

---

## License

MIT — see [LICENSE](LICENSE).
