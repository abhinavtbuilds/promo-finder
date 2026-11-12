"""Deterministic checks on a run's output. No AI here: every check has an exact answer.

    python -m promo_finder.check --run outputs/llm --input data/episodes

Per promo:
  duration_ok      total length is 20-60s
  order_ok         segments are in story order and each has start < end
  timestamps_ok    every start/end is a real timestamp in the screenplay (a line or shot boundary)
  no_midline_cut   no spoken line is cut off at a segment start or end
  hook_event_3s    a line or action begins in the first 3s of the promo (something happens)
  not_duplicate    doesn't overlap a higher-ranked promo by more than half
Warnings (judgement hints, not hard failures):
  opens_on_reply   first spoken line is a short reply ("Dignity?", "No.") to something cut off
  ends_mid_sentence last line trails off ("...") and the sentence continues right after the cut
Writes <run>/check_report.csv and prints a summary.
"""
# Author: Abhinav Tanwar — Promo Finder, Kuku AI Generalist assignment (Nov 2026)
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from .parse import load_show

TOL = 0.06  # seconds


def _quotes_outside(text: str, ep, lo: float, hi: float) -> bool:
    """True if the model's hook/cliffhanger text quotes a line that is NOT spoken inside [lo, hi].

    The model writes these descriptions freely; this catches it quoting a moment the clip does not contain.
    Only quoted fragments of 3+ words are checked; paraphrases cannot be verified this way.
    """
    quotes = [q for q in re.findall(r'["\u201c]([^"\u201d]{8,})["\u201d]', text or "") if len(q.split()) >= 3]
    if not quotes:
        return False
    norm = lambda t: re.sub(r"[^a-z0-9 ]", "", t.lower())
    inside = " ".join(norm(x.text) for x in ep.events if x.kind == "dialogue" and x.end > lo - 0.3 and x.start < hi + 0.3)
    return any(norm(q)[:40] not in inside for q in quotes)


def check_run(run_dir: str, input_dir: str) -> list[dict]:
    eps = {e.number: e for e in load_show(input_dir)}
    pj = Path(run_dir) / "promos.json"
    if not pj.exists():
        raise SystemExit(f"{pj} not found: the run wrote no outputs (see its log for why).")
    promos = json.loads(pj.read_text())["promos"]
    rows, seen = [], []
    for p in promos:
        segs = [(s["episode"], s["start_sec"], s["end_sec"]) for s in p["segments"]]
        total = sum(b - a for _, a, b in segs)
        order_ok = all(a < b for _, a, b in segs) and all(
            (e1, b1) <= (e2, a2) for (e1, _, b1), (e2, a2, _) in zip(segs, segs[1:]))
        ts_ok, mid_ok, hook_ok, reply_warn, trail_warn = True, True, False, False, False
        hook_txt_warn = cliff_txt_warn = False
        for i, (e, a, b) in enumerate(segs):
            ep = eps.get(e)
            if not ep:
                ts_ok = mid_ok = False
                continue
            starts = {round(x.start, 2) for x in ep.events} | {round(s.start, 2) for s in ep.scenes} | {0.0}
            ends = {round(x.end, 2) for x in ep.events} | {round(s.end, 2) for s in ep.scenes} | {round(ep.duration, 2)}
            if not any(abs(a - t) <= TOL for t in starts) or not any(abs(b - t) <= TOL for t in ends):
                ts_ok = False
            for d in (x for x in ep.events if x.kind == "dialogue"):
                if d.start + 0.15 < a < d.end - 0.15 or d.start + 0.15 < b < d.end - 0.15:
                    mid_ok = False
            if i == 0:
                hook_ok = any(a - 0.3 <= x.start < a + 3.0 for x in ep.events if x.kind in ("dialogue", "action", "sfx"))
                hook_txt_warn = _quotes_outside(p.get("hook", ""), ep, a, a + 3.0)
                first = next((x for x in ep.events if x.kind == "dialogue" and x.start >= a - 0.3), None)
                prev = [x for x in ep.events if x.kind == "dialogue" and x.end <= a + 0.3 and x.end > a - 3]
                if first and first.start < a + 3 and len(first.text.split()) <= 2 and not first.text.rstrip().endswith("!") and prev:
                    reply_warn = True
            if i == len(segs) - 1:
                cliff_txt_warn = _quotes_outside(p.get("cliffhanger", ""), ep, b - 5.0, b)
                last = [x for x in ep.events if x.kind == "dialogue" and x.end <= b + 0.3 and x.start >= a]
                nxt = [x for x in ep.events if x.kind == "dialogue" and b - 0.3 <= x.start < b + 1.5]
                if last and last[-1].text.rstrip().endswith(("...", "\u2026")) and nxt and nxt[0].text.startswith(("...", "\u2026")):
                    trail_warn = True
        overlap = 0.0
        for prev in seen:
            ov = sum(max(0.0, min(b1, b2) - max(a1, a2))
                     for e1, a1, b1 in segs for e2, a2, b2 in prev if e1 == e2)
            overlap = max(overlap, ov / max(total, 1e-6))
        seen.append(segs)
        rows.append({
            "rank": p["rank"], "segments": " + ".join(f"Ep{s['episode']} {s['start']}-{s['end']}" for s in p["segments"]),
            "duration_sec": round(total, 1), "duration_ok": 20 <= total <= 60, "order_ok": order_ok,
            "timestamps_ok": ts_ok, "no_midline_cut": mid_ok, "hook_event_3s": hook_ok,
            "not_duplicate": overlap <= 0.5, "max_overlap_with_higher": round(overlap, 2),
            "warn_opens_on_reply": reply_warn, "warn_ends_mid_sentence": trail_warn,
            "warn_hook_text_outside_3s": hook_txt_warn, "warn_cliff_text_outside_5s": cliff_txt_warn,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", default="")
    a = ap.parse_args()
    if not a.input:
        print("ERROR: --input is required (path to the episode screenplays folder). "
              "Example: --input data/episodes")
        return
    if not Path(a.input).is_dir():
        print(f"ERROR: --input '{a.input}' is not a directory or does not exist.")
        return
    rows = check_run(a.run, a.input)
    out = Path(a.run) / "check_report.csv"
    if not rows:
        print(f"0 promos in {a.run}: nothing to check (did the run find any 20-60s windows?)")
        return
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    keys = ["duration_ok", "order_ok", "timestamps_ok", "no_midline_cut", "hook_event_3s", "not_duplicate"]
    print(f"{len(rows)} promos checked -> {out}")
    for k in keys:
        bad = [r["rank"] for r in rows if not r[k]]
        print(f"  {k:16s} {len(rows) - len(bad):4d}/{len(rows)} pass" + (f"   failing ranks: {bad[:15]}" if bad else ""))
    for k in ("warn_opens_on_reply", "warn_ends_mid_sentence", "warn_hook_text_outside_3s", "warn_cliff_text_outside_5s"):
        flagged = [r["rank"] for r in rows if r[k]]
        print(f"  {k:28s} {len(flagged)} flagged" + (f": ranks {flagged[:15]}" if flagged else ""))


if __name__ == "__main__":
    main()
