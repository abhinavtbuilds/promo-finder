"""Compare a run's picks against hand-labelled promos (gold.yaml) and/or another run.

    python -m promo_finder.evaluate --run outputs/llm --gold eval/gold.yaml
    python -m promo_finder.evaluate --run outputs/llm --other outputs/rules_baseline

A pick "hits" a gold promo if, in the same episode, the time overlap is >= 50% of the shorter clip.
Gold labels come from a human (you, or editor approvals exported by the review app).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .pipeline import to_sec


def load_run(path: str) -> list[dict]:
    return json.loads((Path(path) / "promos.json").read_text())["promos"]


def segs(p: dict) -> list[tuple[int, float, float]]:
    return [(s["episode"], s["start_sec"], s["end_sec"]) for s in p["segments"]]


def overlap(a, b) -> float:
    tot = 0.0
    for e1, s1, t1 in a:
        for e2, s2, t2 in b:
            if e1 == e2:
                tot += max(0.0, min(t1, t2) - max(s1, s2))
    la = sum(t - s for _, s, t in a)
    lb = sum(t - s for _, s, t in b)
    return tot / max(min(la, lb), 1e-6)


def load_gold(path: str) -> list[list[tuple[int, float, float]]]:
    items = yaml.safe_load(Path(path).read_text()) or []
    return [[(g["episode"], to_sec(g["start"]), to_sec(g["end"]))] for g in items if g.get("start")]


def evaluate(run: list[dict], gold, ks=(5, 10, 20)) -> dict:
    res = {}
    for k in ks:
        top = [segs(p) for p in run[:k]]
        hits = [any(overlap(t, g) >= 0.5 for g in gold) for t in top]
        found = [any(overlap(t, g) >= 0.5 for t in top) for g in gold]
        res[f"precision@{k}"] = round(sum(hits) / max(len(top), 1), 2)
        res[f"recall@{k}"] = round(sum(found) / max(len(gold), 1), 2)
    all_found = [next((i + 1 for i, p in enumerate(run) if overlap(segs(p), g) >= 0.5), None) for g in gold]
    res["gold_ranks"] = all_found  # where each gold promo landed (None = never proposed)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--gold")
    ap.add_argument("--other", help="second run to measure agreement with")
    a = ap.parse_args()
    run = load_run(a.run)
    if a.gold:
        print(json.dumps(evaluate(run, load_gold(a.gold)), indent=2))
    if a.other:
        other = [segs(p) for p in load_run(a.other)]
        for k in (10, 20):
            agree = sum(any(overlap(segs(p), o) >= 0.5 for o in other[:k]) for p in run[:k])
            print(f"top-{k} agreement with {a.other}: {agree}/{k}")


if __name__ == "__main__":
    main()
