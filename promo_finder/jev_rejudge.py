"""Experiment: re-judge an existing run's promos with TypeSafe's Jev decision model and re-rank them.

The main pipeline's "cold viewer" asks an LLM for 1-10 scores. Jev instead answers yes/no questions with a
probability (a "Noul"), which is closer to how an editor actually decides. This script keeps the clips,
timestamps and hook/cliffhanger text of an existing run unchanged and only replaces the judge, so the
comparison isolates the judge:

  export JEV_KEY=...                       # TypeSafe API key
  python -m promo_finder.jev_rejudge --run outputs/llm --out outputs/llm_jev
  python -m promo_finder.evaluate --run outputs/llm_jev --gold eval/gold.yaml --other outputs/llm

API: POST https://api.typesafe.ai/v1/systemone (docs.typesafe.ai). One request per clip, four Noul questions.
"""
# Author: Abhinav Tanwar — Promo Finder, Kuku AI Generalist assignment (Nov 2026)
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import requests

from .parse import load_show
from .pipeline import write_posting_plan

API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
ROOT = Path(__file__).resolve().parent.parent

CONTEXT = ("This is the full script of a 20-60 second promo clip cut from a vertical microdrama series and posted "
           "on Instagram/TikTok to attract NEW viewers. The viewer sees only this clip. Times are seconds from the "
           "start of the clip.")

QUESTIONS = {
    "hook": {
        "type": "noul",
        "instructions": {"question": "Does something happen in the first 3 seconds that would make a person "
                                     "scrolling past stop and watch?", "first_3_seconds": None},
        "criteria": {"true": "A shocking line, a slap or violence, a scream, or a reveal lands within 3 seconds.",
                     "false": "The opening is calm, scenic, expository, or a reply to a line the viewer did not hear."},
    },
    "unresolved": {
        "type": "noul",
        "instructions": {"question": "Does the clip stop while the moment is still unresolved, so the viewer "
                                     "badly wants to see the next second?", "last_5_seconds": None},
        "criteria": {"true": "It cuts on an open threat, an unanswered question, a raised weapon, or a reveal about "
                             "to land.",
                     "false": "The conflict is settled, someone is rescued or safe, the scene winds down, or it "
                              "simply fades out."},
    },
    "clear": {
        "type": "noul",
        "instructions": "Could a first-time viewer who knows nothing about the show tell, from this clip alone, "
                        "who wants what and why they are in conflict?",
        "criteria": {"true": "The conflict and the stakes are clear from the clip itself.",
                     "false": "It depends on names, objects or events that are never explained in the clip."},
    },
    "spoiler": {
        "type": "noul",
        "instructions": "Does the clip reveal a major twist of the series, such as someone's hidden identity, a "
                        "secret plotter, or a death or return from the dead?",
        "criteria": {"true": "A late-series reveal is shown or stated outright.",
                     "false": "It shows the setup or an everyday conflict, without revealing a twist."},
    },
}


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

    key = os.environ.get("JEV_KEY")
    if not key:
        raise SystemExit("Set JEV_KEY to your TypeSafe API key.")
    src = json.loads((Path(a.run) / "promos.json").read_text())
    promos = src["promos"][: a.limit or None]
    eps_by_num = {e.number: e for e in load_show(a.input)}
    out = Path(a.out)
    (out / "raw_jev").mkdir(parents=True, exist_ok=True)
    cfg_w = {"hook": 0.40, "unresolved": 0.35, "clear": 0.25}

    t0, tokens = time.time(), [0, 0]
    for i, p in enumerate(promos, 1):
        resp = call(build_request(p, eps_by_num), key, out / "raw_jev")
        ans = resp["answers"]
        pr = {k: round(float(ans[k]["noul"]), 3) for k in QUESTIONS}
        u = resp.get("usage") or {}
        tokens[0] += u.get("input_tokens", 0)
        tokens[1] += u.get("output_tokens", 0)
        p["grok_rank"], p["grok_score"] = p["rank"], p["score"]
        p["jev"] = pr
        p["score"] = round(sum(w * pr[k] for k, w in cfg_w.items()), 4)
        # translate into the judge fields the posting plan reads
        p["judge"] = dict(p.get("judge") or {},
                          spoiler="high" if pr["spoiler"] >= 0.7 else "medium" if pr["spoiler"] >= 0.3 else "low",
                          confusing=("Jev: a stranger may not follow it" if pr["clear"] < 0.3 else ""))
        if i % 10 == 0 or i == len(promos):
            print(f"  judged {i}/{len(promos)}")

    # rank by Jev's score; then the same first-batch rule as the main pipeline
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
                jev_calls=len(promos), jev_tokens_in=tokens[0], jev_tokens_out=tokens[1],
                seconds=round(time.time() - t0, 1), rejudged_from=str(a.run))
    out.mkdir(parents=True, exist_ok=True)
    (out / "promos.json").write_text(json.dumps({"meta": meta, "promos": rows}, indent=2))
    import csv
    with open(out / "promos.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "grok_rank", "jev_score", "segments", "duration_sec", "p_hook", "p_unresolved",
                     "p_clear", "p_spoiler", "hook", "cliffhanger", "why"])
        for p in rows:
            j = p["jev"]
            wr.writerow([p["rank"], p["grok_rank"], p["score"],
                         " + ".join(f"Ep{s['episode']} {s['start']}-{s['end']}" for s in p["segments"]),
                         p["duration_sec"], j["hook"], j["unresolved"], j["clear"], j["spoiler"],
                         p["hook"], p["cliffhanger"], p["why"]])
    write_posting_plan(out, rows, meta)
    print(f"Jev re-judged {len(rows)} promos in {meta['seconds']}s "
          f"({tokens[0]} tokens in) -> {out}/ (promos.json, promos.csv, posting_plan.md)")
    print("Biggest moves (Grok rank -> Jev rank):")
    moves = sorted(rows, key=lambda p: abs(p["rank"] - p["grok_rank"]), reverse=True)[:5]
    for p in moves:
        s = p["segments"][0]
        print(f"  Ep{s['episode']} {s['start']}: #{p['grok_rank']} -> #{p['rank']}  {p['jev']}")


if __name__ == "__main__":
    main()
