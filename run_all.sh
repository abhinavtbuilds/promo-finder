#!/usr/bin/env bash
# One command for everything that needs your machine (an LLM API key + internet).
#   export LLM_KEY=...               key for the provider in config.yaml
#   bash run_all.sh
# Optional second model on the first 10 episodes, to show the swap and measure agreement:
#   LLM_KEY2=gsk_... CONFIG2=configs/groq_free.yaml bash run_all.sh
set -euo pipefail
cd "$(dirname "$0")"

[ -d data/episodes ] && ls data/episodes/*.txt >/dev/null 2>&1 || { echo "Put the 50 episode .txt files in data/episodes/ first"; exit 1; }

pip install -q -r requirements.txt
python tests/test_pipeline.py

echo "== 1/3 rules baseline (~30s)"
python -m promo_finder.pipeline --mode rules --out outputs/rules_baseline

echo "== 2/3 LLM run (~7 min; safe to re-run, responses are cached)"
python -m promo_finder.pipeline --mode llm --out outputs/llm

if [ -n "${CONFIG2:-}" ]; then
  echo "== 2b: second model ($CONFIG2) on the first 10 episodes"
  LLM_KEY="${LLM_KEY2:-$LLM_KEY}" python -m promo_finder.pipeline --mode llm --config "$CONFIG2" --out outputs/llm_model2 --limit 10
fi

echo "== 3/3 evaluation"
{
  echo "### rules baseline vs gold"; python -m promo_finder.evaluate --run outputs/rules_baseline --gold eval/gold.yaml
  echo "### LLM run vs gold";       python -m promo_finder.evaluate --run outputs/llm --gold eval/gold.yaml --other outputs/rules_baseline
  if [ -d outputs/llm_model2 ]; then
    echo "### second model (first 10 episodes) vs gold and vs main LLM run"
    python -m promo_finder.evaluate --run outputs/llm_model2 --gold eval/gold.yaml --other outputs/llm --max-episode 10
  fi
} | tee outputs/evaluation.txt

echo "== deterministic checks on every promo"
python -m promo_finder.check --run outputs/rules_baseline | tee outputs/rules_baseline/check_summary.txt
python -m promo_finder.check --run outputs/llm | tee outputs/llm/check_summary.txt
[ -d outputs/llm_model2 ] && python -m promo_finder.check --run outputs/llm_model2 | tee outputs/llm_model2/check_summary.txt
cp eval/spot_check.md eval/gold.yaml outputs/

rm -f raw_outputs.zip && zip -qr raw_outputs.zip outputs
echo
echo "DONE. Send Claude these two files: outputs/evaluation.txt and outputs/llm/report.md"
echo "Upload raw_outputs.zip to Google Drive (share: anyone with the link)."
