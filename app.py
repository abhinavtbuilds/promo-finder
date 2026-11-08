"""Promo Finder web app.

  streamlit run app.py

Tab 1 "Run": upload episode screenplays (.txt or a .zip), pick rules-only or an LLM, run, download results.
Tab 2 "Review": approve / reject promos; download the verdicts (they become evaluation labels).
No screenplay is bundled with the app: users upload their own, and uploads live only in a temp folder.
"""
import csv
import io
import json
import tempfile
import zipfile
from pathlib import Path

import streamlit as st
import yaml

from promo_finder.candidates import script_text
from promo_finder.parse import load_show
from promo_finder.pipeline import run

ROOT = Path(__file__).parent
st.set_page_config(page_title="Promo Finder", page_icon="🎬", layout="wide")
st.title("🎬 Promo Finder")
st.caption("Finds 20–60s promos (hook in the first 3s, cliffhanger at the end, makes sense to a stranger) "
           "in timestamped microdrama screenplays.")

base_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
ss = st.session_state
ss.setdefault("run_dir", None)
ss.setdefault("eps_dir", None)
ss.setdefault("verdicts", {})


def save_uploads(files) -> Path:
    d = Path(tempfile.mkdtemp(prefix="eps_"))
    for f in files:
        if f.name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(f.getvalue())) as z:
                for m in z.namelist():
                    if m.lower().endswith(".txt") and not m.startswith("__MACOSX"):
                        (d / Path(m).name).write_bytes(z.read(m))
        else:
            (d / Path(f.name).name).write_bytes(f.getvalue())
    return d


tab_run, tab_review = st.tabs(["1 · Run", "2 · Review results"])

# ------------------------------------------------------------------ RUN
with tab_run:
    files = st.file_uploader("Episode screenplays (.txt files, or one .zip)", type=["txt", "zip"],
                             accept_multiple_files=True)
    use_sample = st.checkbox("No screenplays handy? Use the built-in sample show (2 invented episodes)",
                             value=False, disabled=bool(files))
    c1, c2 = st.columns(2)
    mode = c1.radio("Mode", ["rules", "llm"], horizontal=True,
                    format_func=lambda m: {"rules": "Rules only (no API, ~20s)",
                                           "llm": "LLM editor + cold-viewer judge"}[m])
    llm_cfg = dict(base_cfg["llm"])
    if mode == "llm":
        cfg_llm = base_cfg["llm"]
        providers = ["openai_compatible", "gemini"]
        provider = c2.selectbox("Provider", providers, index=providers.index(cfg_llm.get("provider", "openai_compatible")),
                                help="openai_compatible = xAI Grok, OpenAI, Groq, OpenRouter, Ollama, or any OpenAI-format API")
        llm_cfg["provider"] = provider
        same = provider == cfg_llm.get("provider")
        llm_cfg["model"] = c2.text_input("Model", cfg_llm["model"] if same else
                                         ("gemini-flash-latest" if provider == "gemini" else "grok-4.3"))
        if provider == "openai_compatible":
            llm_cfg["base_url"] = c2.text_input("Base URL", cfg_llm.get("base_url") or "https://api.x.ai/v1")
        try:
            secret_key = st.secrets.get("LLM_API_KEY", "")
        except Exception:  # no secrets.toml configured
            secret_key = ""
        key = c2.text_input("API key", type="password",
                            help="Used only for this run, kept in memory, never saved." +
                                 (" Leave blank to use the app's key." if secret_key else ""))
        llm_cfg["api_key"] = key or secret_key
        llm_cfg["min_interval_s"] = c2.number_input("Seconds between calls (free tiers need ~4)", 0.0, 30.0,
                                                    float(base_cfg["llm"]["min_interval_s"]))

    if st.button("▶ Find promos", type="primary", disabled=not (files or use_sample)):
        if mode == "llm" and not llm_cfg.get("api_key") and "localhost" not in llm_cfg.get("base_url", ""):
            st.error("Enter an API key, or use Rules only.")
            st.stop()
        eps_dir = save_uploads(files) if files else ROOT / "data" / "sample"
        n = len(list(eps_dir.glob("*.txt")))
        if not n:
            st.error("No .txt screenplays found in the upload.")
            st.stop()
        cfg = dict(base_cfg, llm=llm_cfg)
        out = Path(tempfile.mkdtemp(prefix="run_"))
        bar = st.progress(0.0, text=f"Parsing {n} episodes…")
        try:
            run(str(eps_dir), str(out), mode, cfg=cfg, progress=lambda f, m: bar.progress(min(f, 1.0), text=m))
        except Exception as e:
            st.error(f"Run failed: {e}")
            st.stop()
        ss.run_dir, ss.eps_dir, ss.verdicts = str(out), str(eps_dir), {}
        st.success(f"Done: {n} episodes. Open the **Review results** tab.")

    if ss.run_dir:
        out = Path(ss.run_dir)
        d1, d2, d3 = st.columns(3)
        d1.download_button("promos.json", (out / "promos.json").read_bytes(), "promos.json")
        d2.download_button("promos.csv", (out / "promos.csv").read_bytes(), "promos.csv")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for p in out.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(out))
        d3.download_button("Everything (raw outputs .zip)", buf.getvalue(), "raw_outputs.zip")
        plan = out / "posting_plan.md"
        if plan.exists():
            with st.expander("Posting plan — what to post first, what to hold back", expanded=True):
                st.markdown(plan.read_text().split("\n", 1)[-1].replace("\n## ", "\n#### "))

    st.caption("Built by Abhinav Tanwar for the Kuku AI Generalist assignment (Problem 1, Nov 2026).")
    with st.expander("How it works"):
        st.markdown((ROOT / "README.md").read_text().split("## How it works", 1)[-1].split("## Outputs", 1)[0])

# ------------------------------------------------------------------ REVIEW
with tab_review:
    run_dirs = {}
    if ss.run_dir:
        run_dirs["This session's run"] = (Path(ss.run_dir), Path(ss.eps_dir))
    for p in sorted((ROOT / "outputs").glob("*/promos.json")):  # local saved runs, if any
        run_dirs[f"Saved: {p.parent.name}"] = (p.parent, ROOT / "data" / "episodes")
    if not run_dirs:
        st.info("Run the tool first (tab 1).")
        st.stop()
    label = st.selectbox("Run", list(run_dirs))
    run_dir, eps_dir = run_dirs[label]
    data = json.loads((run_dir / "promos.json").read_text())
    meta, promos = data["meta"], data["promos"]
    st.caption(f"Mode **{meta['mode']}** · model `{meta.get('model', 'none')}` · {meta['episodes']} episodes · "
               f"{len(promos)} promos · LLM calls {meta.get('llm_calls', 0)} · "
               f"tokens in/out {meta.get('tokens_in', 0)}/{meta.get('tokens_out', 0)} · {meta.get('seconds')}s")

    f1, f2 = st.columns([1, 2])
    top_n = f1.slider("Show top", 5, max(5, len(promos)), min(15, len(promos)))
    ep_filter = f2.text_input("Only these episodes (e.g. 3, 11, 12)")
    want = {int(x) for x in ep_filter.replace(" ", "").split(",") if x.isdigit()}
    try:
        eps = {e.number: e for e in load_show(eps_dir)}
    except Exception:
        eps = {}

    for p in promos[:top_n]:
        if want and not (set(p["episodes"]) & want):
            continue
        segs = " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in p["segments"])
        verdict = ss.verdicts.get(p["rank"], {}).get("verdict", "")
        icon = {"approve": "✅ ", "reject": "❌ "}.get(verdict, "")
        with st.expander(f"{icon}#{p['rank']} · {segs} · {p['duration_sec']}s · score {p['score']}",
                         expanded=p["rank"] <= 3):
            a, b = st.columns([3, 2])
            with a:
                st.markdown(f"**Hook:** {p['hook']}  \n**Cliffhanger:** {p['cliffhanger']}  \n**Why:** {p['why']}")
                if p.get("judge"):
                    j = p["judge"]
                    st.caption(f"Cold viewer: hook {j.get('hook')}/10 · cliff {j.get('cliffhanger')}/10 · "
                               f"clarity {j.get('clarity')}/10 · spoiler {j.get('spoiler')} · "
                               f"confusing: {j.get('confusing')}")
            with b:
                comment = st.text_input("Comment", key=f"c{p['rank']}")
                x, y = st.columns(2)
                if x.button("✅ Approve", key=f"a{p['rank']}"):
                    ss.verdicts[p["rank"]] = {"segments": segs, "verdict": "approve", "comment": comment}
                    st.rerun()
                if y.button("❌ Reject", key=f"r{p['rank']}"):
                    ss.verdicts[p["rank"]] = {"segments": segs, "verdict": "reject", "comment": comment}
                    st.rerun()
            if eps:
                st.code("\n\n".join(script_text(eps[s["episode"]], s["start_sec"], s["end_sec"])
                                    for s in p["segments"] if s["episode"] in eps), language=None)

    if ss.verdicts:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["rank", "segments", "verdict", "comment"])
        for r, v in sorted(ss.verdicts.items()):
            w.writerow([r, v["segments"], v["verdict"], v["comment"]])
        st.download_button(f"Download {len(ss.verdicts)} editor verdicts (CSV)", buf.getvalue(), "decisions.csv")
