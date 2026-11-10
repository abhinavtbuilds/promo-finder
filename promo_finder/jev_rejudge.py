"""TypeSafe Jev judge integration for Promo Finder.

Two use cases:

1. **Re-judge an existing run** (standalone script):
     export JEV_KEY=...                       # TypeSafe API key
     python -m promo_finder.jev_rejudge --run outputs/llm --out outputs/llm_jev

2. **Use Jev as the cold-viewer judge in a new pipeline run** (mode="jev"):
     export JEV_KEY=...
     python -m promo_finder.pipeline --input data/episodes --out outputs/jev_run --mode jv

Jev answers yes/no questions with a probability (a "Noul") instead of 1-10 scores, which is closer to
how an editor actually decides. API: POST https://api.typesafe.ai/v1/systemone (docs.typesafe.ai).
"""
# Author: Abhinav Tanwar — Promo Finder, Kuku AI Generalist assignment (Nov 2026)
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .candidates import Candidate
from .parse import Episode, load_show
from .pipeline import clip_script, write_posting_plan

API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
ROOT = Path(__file__).resolve().parent.parent

CONTEXT = ("This is the full script of a 20-60 second promo clip cut from a vertical microdrama series and posted "
           "on Instagram/TikTok to attract NEW viewers. The viewer sees only this clip. Times are seconds from the "
           "start of the clip.")

# Updated questions to match improved cold-viewer criteria (v2 accuracy improvements)
QUESTIONS = {
    "hook": {
        "type": "noul",
        "instructions": {"question": "Does something happen in the first 3 seconds that would make a person "
                                     "scrolling past stop and watch?",
                         "first_3_seconds": None},
        "criteria": {"true": "A shocking line, a slap or violence, a scream, a threat, or a reveal lands within "
                             "3 seconds. An exclamation, a physical action, or a dangerous situation.",
                     "false": "The opening is calm, scenic, expository, or a SHORT REPLY to a line the viewer "
                              "did not hear (e.g. 'Dignity?', 'No.', 'This house...', 'I am.'). "
                              "Also false if the real hook action happened just BEFORE the clip started."},
    },
    "unresolved": {
        "type": "noul",
        "instructions": {"question": "Does the clip stop while the moment is still unresolved, so the viewer "
                                     "badly wants to see the next second?",
                         "last_5_seconds": None},
        "criteria": {"true": "It cuts on an open threat, an unanswered question, a raised weapon, a looming "
                             "danger, or a reveal about to land.",
                     "false": "The conflict is SETTLED — someone is rescued, escapes out a window, hugs another "
                              "character, walks away safely, or the danger passes. Also false for fade outs."},
    },
    "clear": {
        "type": "noul",
        "instructions": "Could a first-time viewer who knows nothing about the show tell, from this clip alone, "
                        "who wants what and why they are in conflict?",
        "criteria": {"true": "The conflict and the stakes are obvious from the clip itself — you can tell who is "
                             "the victim, who is the aggressor, and what is at stake.",
                     "false": "It depends on knowing named characters (Gerald, Lady Juliana, Alexander) or "
                              "past events (the ring, the ledger, after what happened). A stranger would be lost."},
    },
    "spoiler": {
        "type": "noul",
        "instructions": "Does the clip reveal a major twist of the series, such as someone's hidden identity, a "
                        "secret plotter, or a death or return from the dead?",
        "criteria": {"true": "A late-series reveal is shown or stated outright.",
                     "false": "It shows the setup or an everyday conflict, without revealing a twist."},
    },
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def clip_lines(promo: dict, eps_by_num: dict[int, Episode]) -> list[tuple[float, str]]:
    """(seconds-from-clip-start, line) for every event in the promo, across segments."""
    out, offset = [], 0.0
    for seg in promo["segments"]:
        ep = eps_by_num[seg["episode"]]
        a, b = seg["start_sec"], seg["end_sec"]
        for x in ep.events:
            if x.end > a + 0.01 and x.start < b - 0.01:
                who = x.speaker or x.kind.upper()
                tone = f" ({x.tone})" if x.tone else ""
                out.append((round(max(x.start, a) - a + offset, 1), f"{who}{tone}: {x.text}"))
        offset += b - a
    return out


def build_request(promo: dict, eps_by_num: dict[int, Episode]) -> dict:
    """Build a Jev API request for one promo."""
    lines = clip_lines(promo, eps_by_num)
    dur = promo["duration_sec"]
    fmt = lambda ls: "\n".join(f"[{t:4.1f}s] {s}" for t, s in ls) or "(nothing)"
    qs = json.loads(json.dumps(QUESTIONS))
    qs["hook"]["instructions"]["first_3_seconds"] = fmt([l for l in lines if l[0] < 3.0])
    qs["unresolved"]["instructions"]["last_5_seconds"] = fmt([l for l in lines if l[0] >= dur - 5.0])
    state = {"context": CONTEXT, "clip_length_seconds": dur, "script": fmt(lines)}
    return {"model": MODEL, "state": state, "questions": qs}


def call(body: dict, key: str, cache_dir: Path) -> dict:
    """Call Jev API with retry/rate-limit handling. Caches responses."""
    h = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    path = cache_dir / f"jev.{h}.json"
    if path.exists():
        return json.loads(path.read_text())["response"]
    for attempt in range(5):
        r = requests.post(API, json=body, timeout=60,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        if r.status_code in (429, 529):
            wait = 2 ** attempt * 2
            print(f"    (HTTP {r.status_code}, waiting {wait}s)")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            raise SystemExit(f"Jev HTTP {r.status_code}: {r.text[:400]}")
        resp = r.json()
        path.write_text(json.dumps({"model": resp.get("model"), "request_sha1": h, "response": resp}, indent=2))
        return resp
    raise SystemExit("Jev: still rate-limited after 5 tries; rerun later (finished clips are cached).")


def noul_prob(resp: dict, question_key: str) -> float:
    """Extract the probability (0-1) from a Jev Noul answer."""
    return round(float(resp["answers"][question_key]["noul"]), 3)


def spoiler_level(prob: float) -> str:
    return "high" if prob >= 0.7 else "medium" if prob >= 0.3 else "low"


def jev_judge_one(promo: dict, eps_by_num: dict[int, Episode], key: str, cache_dir: Path) -> dict:
    """Judge a single promo with Jev and return the scores dict."""
    resp = call(build_request(promo, eps_by_num), key, cache_dir)
    pr = {k: noul_prob(resp, k) for k in QUESTIONS}
    return {
        "jev": pr,
        "judge": {
            "hook": round(pr["hook"] * 10, 1),      # normalise to 0-10 for compatibility
            "cliffhanger": round(pr["unresolved"] * 10, 1),
            "clarity": round(pr["clear"] * 10, 1),
            "spoiler": spoiler_level(pr["spoiler"]),
            "confusing": ("Jev: a stranger may not follow it" if pr["clear"] < 0.3 else
                          "Jev: reply opening may confuse" if pr["hook"] < 0.3 else ""),
        },
    }


# ---------------------------------------------------------------------------
# Pipeline-compatible judge (drop-in for llm_judge)
# ---------------------------------------------------------------------------

def jev_judge(cands: list[Candidate], eps_by_num: dict[int, Episode], key: str,
              cache_dir: Path, batch: int = 6, progress=None) -> None:
    """Judge a list of Candidate objects with Jev, assigning .judge attributes.

    This is a drop-in replacement for pipeline.llm_judge(). Call it after
    the editing/stitching stage and before ranking.
    """
    progress = progress or (lambda f, m: None)
    for i, c in enumerate(cands):
        # Convert Candidate to a dict-shaped promo for the Jev API
        promo = {
            "segments": [{"episode": s.episode, "start_sec": s.start, "end_sec": s.end} for s in c.segments],
            "duration_sec": c.duration,
        }
        result = jev_judge_one(promo, eps_by_num, key, cache_dir)
        c.judge = result["judge"]
        c.jev_probs = result["jev"]  # raw probabilities for the app to display
        c.source = c.source.replace("llm", "jev") if "llm" in c.source else c.source + "+jev"
        if (i + 1) % max(1, batch) == 0 or i == len(cands) - 1:
            progress(min(0.85 + 0.1 * (i + 1) / len(cands), 0.95),
                     f"Jev judging {i + 1}/{len(cands)} clips")
    progress(0.95, f"Jev judged {len(cands)} clips")


# ---------------------------------------------------------------------------
# Standalone re-judge script (post-hoc)
# ---------------------------------------------------------------------------

def rejudge_run(run_dir: str, out_dir: str, input_dir: str, limit: int = 0) -> Path:
    """Re-judge an existing run's promos with Jev and write the output.

    Returns the output Path.
    """
    key = os.environ.get("JEV_KEY")
    if not key:
        raise SystemExit("Set JEV_KEY to your TypeSafe API key.")

    src = json.loads((Path(run_dir) / "promos.json").read_text())
    promos = src["promos"][: limit or None]
    eps_by_num = {e.number: e for e in load_show(input_dir)}
    out = Path(out_dir)
    (out / "raw_jev").mkdir(parents=True, exist_ok=True)
    cfg_w = {"hook": 0.40, "unresolved": 0.35, "clear": 0.25}

    t0, tokens = time.time(), [0, 0]
    for i, p in enumerate(promos, 1):
        result = jev_judge_one(p, eps_by_num, key, out / "raw_jev")
        resp = result["judge"]
        pr = result["jev"]
        p["original_rank"] = p.get("original_rank", p["rank"])
        p["original_score"] = p.get("original_score", p["score"])
        p["jev"] = pr
        p["score"] = round(sum(w * pr[k] for k, w in cfg_w.items()), 4)
        p["judge"] = resp
        if i % 10 == 0 or i == len(promos):
            print(f"  judged {i}/{len(promos)}")

    # Rank by Jev's score; same first-batch rule as main pipeline
    late_from = src["meta"].get("late_from", 0)
    ranked = sorted(promos, key=lambda p: -p["score"])
    first, rest, used = [], [], set()
    for p in ranked:
        eps = set(p["episodes"])
        late_spoiler = p["judge"]["spoiler"] == "high" and min(eps) >= late_from
        if len(first) < 10 and not (eps & used) and not late_spoiler:
            first.append(p)
            used |= eps
        else:
            rest.append(p)
    rows = first + rest
    for r, p in enumerate(rows, 1):
        p["rank"] = r

    meta = dict(src["meta"], mode="llm + Jev judge", judge_model=MODEL, post_first_n=len(first),
                jev_calls=len(promos), seconds=round(time.time() - t0, 1), rejudged_from=str(run_dir))
    out.mkdir(parents=True, exist_ok=True)
    (out / "promos.json").write_text(json.dumps({"meta": meta, "promos": rows}, indent=2))
    import csv
    with open(out / "promos.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "original_rank", "jev_score", "segments", "duration_sec",
                      "p_hook", "p_unresolved", "p_clear", "p_spoiler", "hook", "cliffhanger", "why"])
        for p in rows:
            j = p["jev"]
            wr.writerow([p["rank"], p.get("original_rank", ""), p["score"],
                         " + ".join(f"Ep{s['episode']} {s['start']}-{s['end']}" for s in p["segments"]),
                         p["duration_sec"], j["hook"], j["unresolved"], j["clear"], j["spoiler"],
                         p["hook"], p["cliffhanger"], p["why"]])
    write_posting_plan(out, rows, meta)
    print(f"Jev re-judged {len(rows)} promos in {meta['seconds']}s -> {out}/")
    print("Biggest moves (original rank -> Jev rank):")
    moves = sorted(rows, key=lambda p: abs(p["rank"] - p.get("original_rank", p["rank"])), reverse=True)[:5]
    for p in moves:
        s = p["segments"][0]
        orig = p.get("original_rank", p["rank"])
        print(f"  Ep{s['episode']} {s['start']}: #{orig} -> #{p['rank']}  {p['jev']}")
    return out


def clip_lines(promo: dict, eps_by_num: dict) -> list[tuple[float, str]]:
    """(seconds-from-clip-start, line) for every event in the clip, across segments."""
    out, offset = [], 0.0
    for seg in promo["segments"]:
        ep = eps_by_num[seg["episode"]]
        a, b = seg["start_sec"], seg["end_sec"]
        for x in ep.events:
            if x.end > a + 0.01 and x.start < b - 0.01:
                who = x.speaker or x.kind.upper()
                tone = f" ({x.tone})" if x.tone else ""
                out.append((round(max(x.start, a) - a + offset, 1), f"{who}{tone}: {x.text}"))
        offset += b - a
    return out


def build_request(promo: dict, eps_by_num: dict) -> dict:
    lines = clip_lines(promo, eps_by_num)
    dur = promo["duration_sec"]
    fmt = lambda ls: "\n".join(f"[{t:4.1f}s] {s}" for t, s in ls) or "(nothing)"
    qs = json.loads(json.dumps(QUESTIONS))
    qs["hook"]["instructions"]["first_3_seconds"] = fmt([l for l in lines if l[0] < 3.0])
    qs["unresolved"]["instructions"]["last_5_seconds"] = fmt([l for l in lines if l[0] >= dur - 5.0])
    state = {"context": CONTEXT, "clip_length_seconds": dur, "script": fmt(lines)}
    return {"model": MODEL, "state": state, "questions": qs}


def call(body: dict, key: str, cache_dir: Path) -> dict:
    h = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    path = cache_dir / f"jev.{h}.json"
    if path.exists():
        return json.loads(path.read_text())["response"]
    for attempt in range(5):
        r = requests.post(API, json=body, timeout=60,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        if r.status_code in (429, 529):
            wait = 2 ** attempt * 2
            print(f"    (HTTP {r.status_code}, waiting {wait}s)")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            raise SystemExit(f"Jev HTTP {r.status_code}: {r.text[:400]}")
        resp = r.json()
        # store the response and a hash of the request, never the request (it contains the script)
        path.write_text(json.dumps({"model": resp.get("model"), "request_sha1": h, "response": resp}, indent=2))
        return resp
    raise SystemExit("Jev: still rate-limited after 5 tries; rerun later (finished clips are cached).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=str(ROOT / "outputs" / "llm"), help="run whose promos to re-judge")
    ap.add_argument("--input", default=str(ROOT / "data" / "episodes"))
    ap.add_argument("--out", default=str(ROOT / "outputs" / "llm_jev"))
    ap.add_argument("--limit", type=int, default=0, help="only the first N promos (smoke test)")
    a = ap.parse_args()
    rejudge_run(a.run, a.out, a.input, limit=a.limit)


if __name__ == "__main__":
    main()
