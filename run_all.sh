#!/usr/bin/env bash
# One command for everything that needs your machine (an LLM API key + internet).
#   bash run_all.sh
set -euo pipefail
cd "$(dirname "$0")"

[ -d data/episodes ] && ls data/episodes/*.txt >/dev/null 2>&1 || { echo "Put the 50 episode .txt files in data/episodes/ first"; exit 1; }

pip install -q -r requirements.txt
python tests/test_pipeline.py

echo "== 1/3 rules baseline (~30s)"
python -m promo_finder.pipeline --mode rules --out outputs/rules_baseline

echo "== 2/3 LLM run (~10-15 min on the free tier; safe to re-run, responses are cached)"
python -m promo_finder.pipeline --mode llm --out outputs/llm

echo "== 3/3 evaluation"
{
  echo "### rules baseline vs gold"; python -m promo_finder.evaluate --run outputs/rules_baseline --gold eval/gold.yaml
  echo "### LLM run vs gold";       python -m promo_finder.evaluate --run outputs/llm --gold eval/gold.yaml --other outputs/rules_baseline
} | tee outputs/evaluation.txt

rm -f raw_outputs.zip && zip -qr raw_outputs.zip outputs
echo
echo "DONE. Send Claude these two files: outputs/evaluation.txt and outputs/llm/report.md"
echo "Upload raw_outputs.zip to Google Drive (share: anyone with the link)."
