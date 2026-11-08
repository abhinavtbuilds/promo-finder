# Promo Finder — finds 20–60s promos in microdrama screenplays

Reads a folder of timestamped screenplays and returns a ranked list of promos: episode(s), start/end of
every segment, the hook, the cliffhanger and one line on why it works.

## Run it

```bash
pip install -r requirements.txt

# 1) No-API baseline (rules only) - runs anywhere in ~20s
python -m promo_finder.pipeline --input data/episodes --out outputs/rules_baseline --mode rules

# 2) Full pipeline with an LLM (free Gemini key from Google AI Studio)
export LLM_KEY=...        # key for the provider in config.yaml (xAI Grok by default)
python -m promo_finder.pipeline --input data/episodes --out outputs/llm --mode llm

# 3) Review / approve picks
streamlit run app.py

# 4) Measure quality against your own labels (eval/gold.yaml) and vs. the baseline
python -m promo_finder.evaluate --run outputs/llm --gold eval/gold.yaml --other outputs/rules_baseline

# 5) Deterministic checks on any run (length, order, real timestamps, no mid-line cuts, duplicates)
python -m promo_finder.check --run outputs/llm

# tests (no API key needed; uses a fake model)
python tests/test_pipeline.py
```

New show? Put its episode `.txt` files in a folder and pass `--input that/folder`. Nothing is show-specific.

## Swap the model

Edit `config.yaml` only. `provider: gemini` or `provider: openai_compatible` (OpenAI, Groq, OpenRouter,
Ollama, vLLM, or any OpenAI-format endpoint Kuku provides: set `base_url`, `model`, `api_key_env`).
LLM responses are cached in `<out>/raw_llm/` keyed by model + prompt, so re-runs are free and every raw
response is kept for audit.

## How it works

| Stage | Who | What |
|---|---|---|
| 1. Parse | code | screenplay → timestamped events (dialogue, action, camera, sfx) |
| 2. Shortlist | code | every legal 20–60s window, snapped to **camera shot boundaries** and never cutting a spoken line; scored by transparent rules (hook energy in first 3s, unresolved ending, two-person conflict, back-references); near-duplicates removed |
| 3. Edit | LLM | per episode, reads the script + rule hints, proposes ≤3 promos with hook / cliffhanger / why |
| 4. Validate | code | snaps LLM times to real cut points, enforces 20–60s, drops anything it can't fix |
| 5. Cold judge | LLM | a "never seen the show" viewer scores each clip **from the clip text only** (hook, cliffhanger, clarity, spoiler) — the standalone test |
| 6. Rank | code | weighted score (weights in `config.yaml`), then posting order: one promo per episode in the first 10, no high-spoiler clips first |
| 7. Review | human | editor approves/rejects in `app.py`; approvals become evaluation labels |

Cross-episode promos: in LLM mode a second LLM pass (`prompts/stitcher.md`) proposes two-part promos (an opening
from one clip + the ending of a later one) because joins need story context; code validates order, cut points and
length. Rules mode uses a simple rule-based stitcher. Duplicates (>50% shared footage) are dropped in code.

Formats: Kuku's block format, or a plain `[mm:ss] NAME: line` format (one event per line, no camera blocks).

Prompts: `prompts/editor.md`, `prompts/cold_viewer.md`.

## Outputs (`outputs/<run>/`)
`promos.json` (everything, incl. judge scores and rule features), `promos.csv`, `report.md` (top 15 with
script excerpts), `raw_llm/` (unedited model responses), `decisions.csv` (editor verdicts).

## Confidentiality
`data/` and `outputs/` are git-ignored: the screenplays are Kuku's and must not be published.
