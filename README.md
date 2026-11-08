# Promo Finder — finds 20–60s promos in microdrama screenplays

Reads a folder of timestamped screenplays and returns a ranked list of promos: episode(s), start/end of
every segment, the hook, the cliffhanger and one line on why it works.

## Run it

```bash
pip install -r requirements.txt

# 1) No-API baseline (rules only) - runs anywhere in ~20s
python -m promo_finder.pipeline --input data/episodes --out outputs/rules_baseline --mode rules

# 2) Full pipeline with an LLM. The provider/model is set in config.yaml (xAI Grok by default);
#    ready-made free-tier configs are in configs/ (Groq, OpenRouter, Gemini, local Ollama).
export LLM_KEY=...        # key for whichever provider the config names
python -m promo_finder.pipeline --input data/episodes --out outputs/llm --mode llm
#    quick demo or free-tier slice: add  --limit 10  (first 10 episodes)

# 3) Review / approve picks
streamlit run app.py

# 4) Measure quality against your own labels (eval/gold.yaml) and vs. the baseline
python -m promo_finder.evaluate --run outputs/llm --gold eval/gold.yaml --other outputs/rules_baseline

# 5) Deterministic checks on any run (length, order, real timestamps, no mid-line cuts, duplicates)
python -m promo_finder.check --run outputs/llm

# 6) Swap the model and compare: same code, different brain
LLM_KEY=gsk_... python -m promo_finder.pipeline --config configs/groq_free.yaml --out outputs/llm_groq --mode llm --limit 10
python -m promo_finder.evaluate --run outputs/llm_groq --gold eval/gold.yaml --other outputs/llm --max-episode 10

# tests (no API key needed; uses a fake model)
python tests/test_pipeline.py
```

New show? Put its episode `.txt` files in a folder and pass `--input that/folder`. Nothing is show-specific.

## Swap the model

Edit `config.yaml` only, or pass `--config configs/<provider>.yaml`. `provider: gemini` or
`provider: openai_compatible` (OpenAI, Groq, OpenRouter, Ollama, vLLM, or any OpenAI-format endpoint Kuku
provides: set `base_url`, `model`, `api_key_env`). Prompts and code do not change.
LLM responses are cached in `<out>/raw_llm/` keyed by model + prompt hash, so re-runs are free and every raw
response is kept for audit. The cache stores responses only, never the screenplay, so raw outputs can be
shared without sharing the show.

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
`promos.json` (everything, incl. judge scores and rule features), `promos.csv`, `posting_plan.md` (what to
post first with caption hooks, what to hold back and why), `report.md` (top 15 with script excerpts),
`raw_llm/` (unedited model responses), `check_report.csv` (deterministic checks), `decisions.csv` (editor verdicts).

## Confidentiality
`data/` and `outputs/` are git-ignored: the screenplays are Kuku's and must not be published.
