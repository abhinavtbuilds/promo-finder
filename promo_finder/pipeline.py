"""End-to-end promo finder.

  python -m promo_finder.pipeline --input data/episodes --out outputs/run --mode llm
  python -m promo_finder.pipeline --input data/episodes --out outputs/rules --mode rules   # no API needed

Stages
  1. parse          (code)   screenplay -> timestamped events
  2. shortlist      (code)   all legal 20-60s windows -> rule scores -> top-K per episode
  3. edit           (LLM)    per episode: pick/adjust promos, write hook / cliffhanger / why
  4. validate       (code)   snap to shot boundaries, enforce 20-60s, drop anything invalid
  5. cold judge     (LLM)    a "never seen the show" viewer scores each clip from the clip text ONLY
  6. rank           (code)   transparent weights + posting-order rules (diversity, spoilers)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from .candidates import (MAX_LEN, MIN_LEN, Candidate, Segment, cut_points, episode_candidates,
                         score_window, script_text, stitched_candidates)
from .parse import Episode, fmt, load_show

ROOT = Path(__file__).resolve().parent.parent
TS_RE = re.compile(r"(\d+):(\d+(?:\.\d+)?)")


def to_sec(s: str) -> float | None:
    m = TS_RE.search(str(s))
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


# ------------------------------------------------------------------ validation (code, not AI)
def snap_window(ep: Episode, start: float, end: float) -> tuple[float, float] | None:
    """Snap LLM-proposed times to the nearest legal cut points and enforce 20-60s."""
    starts, ends = cut_points(ep)
    a = min(starts, key=lambda t: abs(t - start))
    legal_ends = [t for t in ends if MIN_LEN <= t - a <= MAX_LEN]
    if not legal_ends:
        return None
    b = min(legal_ends, key=lambda t: abs(t - end))
    if abs(a - start) > 4 or abs(b - end) > 6:   # model's times were far off: don't silently invent a clip
        return None
    return a, b


# ------------------------------------------------------------------ LLM stages
class Cache:
    """Disk cache keyed by prompt hash: reruns are free, and raw responses are kept for review."""

    def __init__(self, folder: Path, model_tag: str):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.tag = model_tag

    def get_or_call(self, name: str, client, system: str, user: str):
        h = hashlib.sha1(f"{self.tag}\n{system}\n{user}".encode()).hexdigest()[:10]
        path = self.folder / f"{name}.{h}.json"
        if path.exists():
            d = json.loads(path.read_text())
            if "prompt" in d:  # older cache files kept the full prompt (= the screenplay); strip it
                d.pop("prompt")
                d["prompt_sha1"] = h
                path.write_text(json.dumps(d, indent=2))
            return d["response"]
        resp = client.json(system, user)
        # Store the response and a hash of the prompt, never the prompt itself: the screenplay is
        # Kuku's confidential content and raw_llm/ is shared as part of the raw outputs.
        path.write_text(json.dumps({"model": self.tag, "prompt_sha1": h, "response": resp}, indent=2))
        return resp


def story_so_far(eps: list[Episode], upto: int, n: int = 6) -> str:
    prev = [e for e in eps if e.number < upto][-n:]
    return "\n".join(f"Ep {e.number}: {e.logline}" for e in prev) or "(first episode)"


def llm_edit(ep: Episode, eps: list[Episode], hints: list[Candidate], client, cache: Cache, cfg: dict) -> list[Candidate]:
    system = (ROOT / "prompts" / "editor.md").read_text().replace("{max_promos}", str(cfg["max_promos_per_episode"]))
    hint_txt = "\n".join(f"- {fmt(c.segments[0].start)}-{fmt(c.segments[0].end)} (rule score {c.rule_score})" for c in hints)
    user = (f"STORY SO FAR (context only):\n{story_so_far(eps, ep.number)}\n\n"
            f"EPISODE {ep.number} LOGLINE: {ep.logline}\n\n"
            f"RULE-BASED HINTS:\n{hint_txt}\n\n"
            f"SCREENPLAY (episode {ep.number}, {fmt(ep.duration)} long):\n{script_text(ep, 0, ep.duration + 1)}")
    resp = cache.get_or_call(f"ep{ep.number:03d}_editor", client, system, user)
    out = []
    for p in (resp.get("promos", []) if isinstance(resp, dict) else []):
        s, e = to_sec(p.get("start")), to_sec(p.get("end"))
        if s is None or e is None:
            continue
        snapped = snap_window(ep, s, e)
        if not snapped:
            print(f"  ep {ep.number}: dropped invalid LLM window {p.get('start')}-{p.get('end')}")
            continue
        c = score_window(ep, *snapped)  # keep rule features for comparison
        c.source = "llm"
        c.hook_text = p.get("hook", c.hook_text)
        c.cliff_text = p.get("cliffhanger", c.cliff_text)
        c.notes = [f"why: {p.get('why', '')}", f"standalone risk: {p.get('standalone_risk', '')}"]
        if abs(snapped[0] - s) > 0.05 or abs(snapped[1] - e) > 0.05:
            c.notes.append(f"snapped from {p.get('start')}-{p.get('end')}")
        out.append(c)
    return out


def llm_stitch(picks: list[Candidate], eps_by_num, client, cache: Cache, cfg: dict) -> list[Candidate]:
    """Pass 2 (cross-episode joins need story context, so the model proposes them; code validates)."""
    singles = [c for c in picks if len(c.segments) == 1]
    if len(singles) < 2:
        return []
    system = (ROOT / "prompts" / "stitcher.md").read_text().replace("{max_stitches}", str(cfg.get("llm_stitches", 6)))
    lines = []
    for i, c in enumerate(singles):
        sg = c.segments[0]
        lines.append(f"[{i}] Ep {sg.episode} {fmt(sg.start)}-{fmt(sg.end)} | hook: {c.hook_text[:160]} | "
                     f"cliffhanger: {c.cliff_text[:160]}")
    resp = cache.get_or_call("stitcher", client, system, "CLIPS (story order):\n" + "\n".join(lines))
    out = []
    for st in (resp.get("stitches", []) if isinstance(resp, dict) else []):
        try:
            A, B = singles[int(st["a"])], singles[int(st["b"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        sa, sb = A.segments[0], B.segments[0]
        ea, eb = eps_by_num[sa.episode], eps_by_num[sb.episode]
        a_end_req, b_start_req = to_sec(st.get("a_end")), to_sec(st.get("b_start"))
        if a_end_req is None or b_start_req is None:
            continue
        _, a_ends = cut_points(ea)
        b_starts, _ = cut_points(eb)
        a_end = min((t for t in a_ends if sa.start + 6 <= t <= sa.end), key=lambda t: abs(t - a_end_req), default=None)
        b_start = min((t for t in b_starts if sb.start <= t < sb.end - 5), key=lambda t: abs(t - b_start_req), default=None)
        if a_end is None or b_start is None:
            continue
        segs = [Segment(sa.episode, sa.start, a_end), Segment(sb.episode, b_start, sb.end)]
        total = sum(x.duration for x in segs)
        if not (MIN_LEN <= total <= MAX_LEN) or (sb.episode, b_start) <= (sa.episode, a_end):
            print(f"  stitch {st.get('a')}+{st.get('b')}: dropped (length {total:.0f}s or order)")
            continue
        c = Candidate(segments=segs, hook_text=st.get("hook", A.hook_text), cliff_text=st.get("cliffhanger", B.cliff_text),
                      notes=[f"why: {st.get('why', '')}", "stitched by LLM"], source="llm-stitch")
        c.rule_score = (A.rule_score + B.rule_score) / 2
        out.append(c)
    return out


def clip_script(c: Candidate, eps_by_num: dict[int, Episode]) -> str:
    parts = []
    for i, seg in enumerate(c.segments):
        ep = eps_by_num[seg.episode]
        if len(c.segments) > 1:
            parts.append(f"--- segment {i + 1} ---")
        # re-base timestamps to the clip so the judge sees what a viewer sees (0s = clip start)
        txt = script_text(ep, seg.start, seg.end)
        parts.append(txt)
    return "\n".join(parts)


def llm_judge(cands: list[Candidate], eps_by_num, client, cache: Cache, batch: int = 6) -> None:
    system = (ROOT / "prompts" / "cold_viewer.md").read_text()
    for i in range(0, len(cands), batch):
        chunk = cands[i:i + batch]
        user = "\n\n".join(
            f"=== CLIP id={j} ({c.duration:.0f}s; clip starts at {fmt(c.segments[0].start)}) ===\n{clip_script(c, eps_by_num)}"
            for j, c in enumerate(chunk))
        resp = cache.get_or_call(f"judge_{i:04d}", client, system, user)
        by_id = {str(x.get("id")): x for x in (resp.get("clips", []) if isinstance(resp, dict) else [])}
        for j, c in enumerate(chunk):
            v = by_id.get(str(j))
            if v:
                c.judge = {k: v.get(k) for k in ("hook", "cliffhanger", "clarity", "spoiler", "confusing")}


# ------------------------------------------------------------------ ranking (code)
def final_score(c: Candidate, w: dict, max_rule: float) -> float:
    j = getattr(c, "judge", None)
    rule = max(c.rule_score, 0) / max_rule if max_rule else 0
    if j and all(isinstance(j.get(k), (int, float)) for k in ("hook", "cliffhanger", "clarity")):
        judged = (w["hook"] * j["hook"] + w["cliffhanger"] * j["cliffhanger"] + w["clarity"] * j["clarity"]) / 10
        return round((1 - w["rule_blend"]) * judged + w["rule_blend"] * rule, 4)
    return round(rule, 4)


def posting_order(ranked: list[Candidate], top_n: int = 10, late_from: int = 0) -> list[Candidate]:
    """Greedy: best first, but at most one promo per episode and no high-spoiler clips in the first batch.

    The cold viewer only sees the clip, not where it sits in the series, so it also flags the show's
    *premise* (ep 3's proposal) as a "twist". A spoiler flag therefore only counts for clips from the
    back part of the show (episode >= late_from); early-episode setup is what promos should sell.
    """
    first, rest, used = [], [], set()
    for c in ranked:
        eps = {s.episode for s in c.segments}
        spoiler = ((getattr(c, "judge", None) or {}).get("spoiler") == "high"
                   and min(eps) >= late_from)
        if len(first) < top_n and not (eps & used) and not spoiler:
            first.append(c)
            used |= eps
        else:
            rest.append(c)
    return first + rest


def _first_batch_size(ordered: list[Candidate], top_n: int, late_from: int) -> int:
    """How many leading promos satisfy the first-batch rules (one per episode, no late spoilers)."""
    used, n = set(), 0
    for c in ordered[:top_n]:
        eps = {s.episode for s in c.segments}
        spoiler = (getattr(c, "judge", None) or {}).get("spoiler") == "high" and min(eps) >= late_from
        if eps & used or spoiler:
            break
        used |= eps
        n += 1
    return n


def why_line(c: Candidate) -> str:
    for n in c.notes:
        if n.startswith("why:"):
            return n[4:].strip()
    bits = []
    if c.hook_score >= 1.5:
        bits.append("high-energy opening")
    if c.cliff_score >= 1.5:
        bits.append("ends unresolved")
    if c.standalone_score >= 1.1:
        bits.append("two-person conflict readable without context")
    return "(rules) " + (", ".join(bits) or "best available window in this episode")


# ------------------------------------------------------------------ outputs
def write_outputs(out: Path, ordered: list[Candidate], eps_by_num, meta: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for rank, c in enumerate(ordered, 1):
        j = getattr(c, "judge", None) or {}
        rows.append({
            "rank": rank,
            "score": c.final,
            "episodes": sorted({s.episode for s in c.segments}),
            "segments": [{"episode": s.episode, "start": fmt(s.start), "end": fmt(s.end),
                          "start_sec": round(s.start, 2), "end_sec": round(s.end, 2)} for s in c.segments],
            "duration_sec": round(c.duration, 1),
            "hook": c.hook_text,
            "cliffhanger": c.cliff_text,
            "why": why_line(c),
            "source": c.source,
            "judge": j,
            "rule_features": {"hook": c.hook_score, "cliff": c.cliff_score, "standalone": c.standalone_score,
                              "energy": c.energy_score, "rule_score": c.rule_score},
            "notes": c.notes,
        })
    (out / "promos.json").write_text(json.dumps({"meta": meta, "promos": rows}, indent=2))
    with open(out / "promos.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "score", "segments", "duration_sec", "hook", "cliffhanger", "why",
                     "judge_hook", "judge_cliff", "judge_clarity", "spoiler", "confusing", "source"])
        for r in rows:
            j = r["judge"]
            wr.writerow([r["rank"], r["score"], " + ".join(f"Ep{s['episode']} {s['start']}-{s['end']}" for s in r["segments"]),
                         r["duration_sec"], r["hook"], r["cliffhanger"], r["why"], j.get("hook"), j.get("cliffhanger"),
                         j.get("clarity"), j.get("spoiler"), j.get("confusing"), r["source"]])
    md = [f"# Promo picks — {meta['mode']} mode", "",
          f"Model: `{meta.get('model', 'none')}` · episodes: {meta['episodes']} · candidates judged: {len(rows)} · "
          f"LLM calls: {meta.get('llm_calls', 0)} · tokens in/out: {meta.get('tokens_in', 0)}/{meta.get('tokens_out', 0)}", ""]
    for r, c in zip(rows[:meta.get("report_top", 15)], ordered):
        segs = " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in r["segments"])
        md += [f"## #{r['rank']} · {segs} · {r['duration_sec']}s · score {r['score']}", "",
               f"- **Hook:** {r['hook']}", f"- **Cliffhanger:** {r['cliffhanger']}", f"- **Why:** {r['why']}"]
        if r["judge"]:
            j = r["judge"]
            md.append(f"- **Cold viewer:** hook {j.get('hook')}/10 · cliff {j.get('cliffhanger')}/10 · clarity "
                      f"{j.get('clarity')}/10 · spoiler {j.get('spoiler')} · confusing: {j.get('confusing')}")
        md += ["", "<details><summary>Script excerpt</summary>", "", "```", clip_script(c, eps_by_num), "```", "</details>", ""]
    (out / "report.md").write_text("\n".join(md))
    write_posting_plan(out, rows, meta)


def write_posting_plan(out: Path, rows: list[dict], meta: dict) -> None:
    """What an editor actually acts on: what to post this week, in order, and what to hold back and why."""
    n = meta.get("post_first_n", 10)
    late_from = meta.get("late_from", 0)
    first = rows[:n]
    held = [r for r in rows[n:] if (r["judge"].get("spoiler") == "high" and min(r["episodes"]) >= late_from)]
    flagged = [r for r in rows[n:] if r["judge"].get("confusing")][:5]
    seg = lambda r: " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in r["segments"])
    def cap(t: str) -> str:  # drop [time] and (tone); a camera/action hook is labelled, not posted as text
        t = re.sub(r"\s*\[[^\]]*\]\s*|\s*\([^)]*\)", " ", t).replace(" :", ":").replace("  ", " ").strip()
        if re.match(r"^(CAMERA|ACTION|SFX|SOUND|VISUAL)\b", t, re.I):
            return "_(visual hook, write a caption)_ " + t.split(":", 1)[-1].strip()[:70]
        return t
    md = [f"# Posting plan — {meta['mode']} mode, {meta['episodes']} episodes", "",
          f"Post these {len(first)} first, in this order: the best promo from each of {len(first)} different episodes, "
          "no late-series spoilers. "
          "Caption = the hook line; post one a day and watch which hooks convert.", "",
          "| # | Clip | Length | Caption (hook) | Why this one |", "|---|---|---|---|---|"]
    for r in first:
        md.append(f"| {r['rank']} | {seg(r)} | {r['duration_sec']}s | {cap(r['hook'])} | {r['why']} |")
    if len(rows) > n:
        md += ["", f"Next in line: {len(rows) - n} more promos, ranked, in promos.csv."]
    md += ["", f"## Hold back until the show has an audience ({len(held)})",
           f"Clips from episode {late_from}+ that the cold viewer rated a high spoiler. Good promos, wrong week.", ""]
    md += [f"- #{r['rank']} {seg(r)} — ends on: {r['cliffhanger']}" for r in held] or ["- none"]
    md += ["", f"## Needs an editor's eye ({len(flagged)} shown)",
           "The cold viewer found these confusing without context; re-cut or skip.", ""]
    md += [f"- #{r['rank']} {seg(r)} — {r['judge'].get('confusing')}" for r in flagged] or ["- none"]
    md += ["", "Human step: approve/reject in the review app; approvals become labels for the next run."]
    (out / "posting_plan.md").write_text("\n".join(md))


# ------------------------------------------------------------------ main
def run(input_dir: str, out_dir: str, mode: str, config_path: str | None = None,
        cfg: dict | None = None, progress=None, limit: int = 0) -> Path:
    """progress: optional callback(fraction, message) for UIs."""
    cfg = cfg or yaml.safe_load(Path(config_path or ROOT / "config.yaml").read_text())
    progress = progress or (lambda f, m: None)
    t0 = time.time()
    eps = load_show(input_dir)
    if limit:
        eps = eps[:limit]  # first N episodes: quick demos and free-tier runs
    eps_by_num = {e.number: e for e in eps}
    out = Path(out_dir)
    print(f"Parsed {len(eps)} episodes from {input_dir}")

    shortlist = {ep.number: episode_candidates(ep, keep=cfg["shortlist_per_episode"]) for ep in eps}
    stitches = (stitched_candidates(eps, keep=cfg["stitched_candidates"])
                if cfg.get("stitched_candidates") and mode == "rules" else [])

    meta = {"mode": mode, "episodes": len(eps), "input": str(input_dir), "report_top": cfg.get("report_top", 15)}
    if mode == "rules":
        pool = [c for cs in shortlist.values() for c in cs[: cfg["max_promos_per_episode"]]] + stitches
    else:
        from .llm import LLMClient, LLMConfig
        lc = LLMConfig(**cfg["llm"])
        client = LLMClient(lc)
        cache = Cache(out / "raw_llm", f"{lc.provider}:{lc.model}")
        pool = []
        for k, ep in enumerate(eps):
            print(f"  editing ep {ep.number} ...")
            progress(0.8 * k / len(eps), f"LLM editing episode {ep.number} ({k + 1}/{len(eps)})")
            picks = llm_edit(ep, eps, shortlist[ep.number], client, cache, cfg)
            pool += picks or shortlist[ep.number][:1]  # fall back to best rule pick if the model returns nothing
        progress(0.82, "LLM stitching cross-episode promos")
        print("  stitching cross-episode promos ...")
        pool += llm_stitch(pool, eps_by_num, client, cache, cfg)
        print(f"  cold-viewer judging {len(pool)} clips ...")
        progress(0.85, f"Cold-viewer judging {len(pool)} clips")
        llm_judge(pool, eps_by_num, client, cache, batch=cfg.get("judge_batch", 6))
        meta.update(model=f"{lc.provider}:{lc.model}", llm_calls=client.calls,
                    tokens_in=client.tokens_in, tokens_out=client.tokens_out)

    max_rule = max((c.rule_score for c in pool), default=1)
    for c in pool:
        c.final = final_score(c, cfg["weights"], max_rule)
    ranked = sorted(pool, key=lambda c: c.final, reverse=True)
    # de-duplicate in code: drop a promo that is mostly the same footage as a higher-ranked one
    from .candidates import _overlap
    deduped = []
    for c in ranked:
        if all(_overlap(c, k) <= 0.5 for k in deduped):
            deduped.append(c)
    meta["dropped_duplicates"] = len(ranked) - len(deduped)
    ranked = deduped
    last_ep = max((e.number for e in eps), default=1)
    late_from = int(last_ep * cfg.get("spoiler_only_after_fraction", 0.5)) + 1  # 50 eps -> ep 26 onwards
    ordered = posting_order(ranked, cfg.get("post_first_n", 10), late_from)
    first_batch = _first_batch_size(ordered, cfg.get("post_first_n", 10), late_from)
    meta.update(post_first_n=first_batch, late_from=late_from,
                seconds=round(time.time() - t0, 1))
    write_outputs(out, ordered, eps_by_num, meta)
    progress(1.0, "Done")
    print(f"Wrote {len(ordered)} promos to {out}/ (promos.json, promos.csv, report.md, posting_plan.md) in {meta['seconds']}s")
    return out


def main():
    ap = argparse.ArgumentParser(description="Find promo clips in microdrama screenplays")
    ap.add_argument("--input", default="data/episodes", help="folder of episode .txt screenplays")
    ap.add_argument("--out", default="outputs/run")
    ap.add_argument("--mode", choices=["llm", "rules"], default="llm")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--limit", type=int, default=0, help="only the first N episodes (demo / free-tier runs)")
    a = ap.parse_args()
    run(a.input, a.out, a.mode, a.config, limit=a.limit)


if __name__ == "__main__":
    main()
