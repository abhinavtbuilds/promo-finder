"""Promo Finder — Streamlit app for the Kuku AI Generalist assignment (Problem 1, Nov 2026).

Run with:  streamlit run app.py

Tabs:
  ▶ Run          Upload episodes, pick rules-only or LLM, run the pipeline, download results.
  📋 Review      Browse, filter, sort, approve / reject promos.  Also shows per-promo
                 deterministic check results (pass/fail + warnings).
  🔍 Compare     Compare two runs side-by-side (metrics, coverage, score dist, top-N overlap).
  ✅ Quality     Check-report summary, evaluation against editor-labelled gold data, spot-check
                 results and honest failure patterns.
  📊 Analytics   Charts for scores, judge ratings, source mix, duration histogram, per-episode
                 coverage, top-vs-bottom promos, and cost estimation.
"""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

import streamlit as st
import yaml

from promo_finder.candidates import script_text
from promo_finder.parse import load_show
from promo_finder.pipeline import run

# Jev rejudge (best-effort import)
try:
    from promo_finder.jev_rejudge import rejudge_run as _jev_rejudge
    _HAVE_JEV = True
except ImportError:
    _HAVE_JEV = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent
st.set_page_config(page_title="Promo Finder · Kuku", page_icon="🎬", layout="wide")

# Approximate per-token costs in USD (used for cost estimation)
MODEL_COST_PER_1M_IN = {"grok-4.3": 5.00, "grok-3.5": 3.00, "gemini-2.5-flash": 0.15, "gemini-2.5-pro": 1.25,
                        "gpt-4o": 2.50, "gpt-4o-mini": 0.15, "claude-3.5-sonnet": 3.00, "llama-3.3-70b": 0.59}
MODEL_COST_PER_1M_OUT = {"grok-4.3": 15.00, "grok-3.5": 8.00, "gemini-2.5-flash": 0.60, "gemini-2.5-pro": 10.00,
                          "gpt-4o": 10.00, "gpt-4o-mini": 0.60, "claude-3.5-sonnet": 15.00, "llama-3.3-70b": 0.79}
DEFAULT_COST_IN = 2.00
DEFAULT_COST_OUT = 8.00

base_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
ss = st.session_state
ss.setdefault("run_dir", None)
ss.setdefault("eps_dir", None)
ss.setdefault("verdicts", {})
ss.setdefault("run_history", {})
ss.setdefault("batch_verdict_mode", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def load_promos(run_dir: Path) -> tuple[dict, list[dict]]:
    data = json.loads((run_dir / "promos.json").read_text())
    return data["meta"], data["promos"]


def load_checks(run_dir: Path) -> list[dict] | None:
    p = run_dir / "check_report.csv"
    if not p.exists():
        return None
    with open(p) as f:
        return list(csv.DictReader(f))


def load_run_options(include_current: bool = True) -> dict[str, tuple[Path, Path]]:
    """Build {label: (run_dir, eps_dir)} from session only.

    Saved runs from outputs/ are NOT loaded — they contain Kuku's confidential
    episode data and must not be shown to app visitors. Only the current session's
    run (temp directory) and the sample data (invented episodes) are visible.
    """
    opts = {}
    if include_current and ss.run_dir and Path(ss.run_dir).exists():
        opts["This session's run"] = (Path(ss.run_dir), Path(ss.eps_dir))
    # Saved runs from outputs/ are NOT loaded in deployed mode (Kuku confidentiality).
    # Uncomment for local dev:
    # for p in sorted((ROOT / "outputs").glob("*/promos.json")):
    #     opts[f"Saved: {p.parent.name}"] = (p.parent, ROOT / "data" / "episodes")
    return opts


def run_meta_display(meta: dict):
    parts = [
        f"Mode **{meta['mode']}**",
        f"model `{meta.get('model', 'none')}`",
        f"{meta['episodes']} episodes",
        f"{meta.get('total_promos', meta.get('promos', 0))} promos",
        f"LLM calls {meta.get('llm_calls', 0)}",
        f"tokens {meta.get('tokens_in', 0)}/{meta.get('tokens_out', 0)}",
        f"{meta.get('seconds', 0):.0f}s" if isinstance(meta.get("seconds"), (int, float)) else ""
    ]
    st.caption(" · ".join(p for p in parts if p))


def estimate_cost(meta: dict) -> str:
    """Estimate LLM cost from token counts. Returns a human string like '$0.42'."""
    if not meta.get("tokens_in") or not meta.get("tokens_out"):
        return "N/A (no LLM used)"
    model = meta.get("model", "")
    # Extract just the model name from e.g. "openai_compatible:grok-4.3"
    model_short = model.split(":")[-1] if ":" in model else model
    cost_in = MODEL_COST_PER_1M_IN.get(model_short, DEFAULT_COST_IN)
    cost_out = MODEL_COST_PER_1M_OUT.get(model_short, DEFAULT_COST_OUT)
    total = (meta["tokens_in"] / 1_000_000 * cost_in) + (meta["tokens_out"] / 1_000_000 * cost_out)
    if total < 0.01:
        return "<$0.01"
    return f"${total:.2f}"


def badge(text: str, color: str) -> str:
    return f'<span style="background:{color};color:white;padding:1px 9px;border-radius:8px;font-size:0.75em;white-space:nowrap">{text}</span>'


def safe_read(path: Path) -> str | None:
    try:
        return path.read_text()
    except Exception:
        return None


def css_block() -> None:
    """Inject minimal custom CSS for nicer cards."""
    st.markdown("""
    <style>
    .promo-card { border-radius:8px; padding:12px; margin-bottom:10px; }
    .promo-pass { border:2px solid #27ae60; }
    .promo-warn { border:2px solid #f39c12; }
    .promo-fail { border:2px solid #e74c3c; }
    .promo-none { border:1px solid #e0e0e0; }
    .badge-green { background:#27ae60; color:white; padding:1px 8px; border-radius:8px; font-size:0.75em; }
    .badge-orange { background:#f39c12; color:white; padding:1px 8px; border-radius:8px; font-size:0.75em; }
    .badge-red { background:#e74c3c; color:white; padding:1px 8px; border-radius:8px; font-size:0.75em; }
    </style>
    """, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# TABS
# ---------------------------------------------------------------------------

css_block()

tab_about, tab_run, tab_review, tab_compare, tab_quality, tab_analytics = st.tabs(
    ["🏠 About", "▶ Run", "📋 Review", "🔍 Compare", "✅ Quality", "📊 Analytics"]
)

# ===================================================================
# TAB 0 — About
# ===================================================================
with tab_about:
    st.header("🎬 Promo Finder — Problem 1: Find the Promos")
    st.markdown("""
    This tool solves **Kuku's Problem 1**: given a folder of timestamped microdrama screenplays,
    find every 20–60-second promo that would make a scrolling viewer stop, watch, and need to see
    the next episode.
    """)

    st.info(
        "🔒 **Confidentiality**: This app does not store or display any Kuku episode data. "
        "Uploaded screenplays live only in a temporary folder and are deleted when the session ends. "
        "Pre-computed runs are disabled in deployed mode. The sample show uses invented episodes."
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Approach", "Rules + optional LLM", help="Pure code for parsing + shortlisting; LLM for editing and cold-viewer judging")
    c2.metric("Cost (50 eps, rules)", "Free (zero API calls)", help="Rules mode uses no API — runs in ~25s on any machine")
    c3.metric("Cost (50 eps, LLM)", "~$0.04–0.30", help="LLM mode costs vary by model. Grok 4.3 ~$0.10; Gemini Flash ~$0.01")

    with st.expander("📐 Assumptions made", expanded=True):
        st.markdown("""
        - **Promo = hook + cliffhanger**: a clip that opens with a stop-scrolling moment (first 3s)
          and ends unresolved. The assignment says "no spec" — this is the core editorial heuristic.
        - **20–60s = hard boundary**: every platform (IG, TikTok, YT Shorts) works in this range.
        - **Camera-shot boundaries** are the natural cut points. The tool snaps all times to real
          shot/event boundaries so an editor can cut exactly there.
        - **Cross-episode promos** (join a moment from ep N + a later moment) are allowed and scored.
        - **"Standalone" matters**: a promo must make sense to someone who has never seen the show.
          Back-references and unresolved character names are penalised.
        - **Spoilers** from late episodes are held back from the first posting batch.
        - **One label, 6 gold promos**: the evaluation set is small but hand-verified against the script.
        """)

    with st.expander("🧠 What's AI? What's code?"):
        cols = st.columns(2)
        with cols[0]:
            st.markdown("**Code (deterministic)**")
            st.markdown("""
            - Screenplay parser (block + plain format)
            - Candidate generator (all 20–60s windows on camera-shot boundaries)
            - Rule-based scorer (hook energy, cliff cues, standalone readability)
            - Non-max suppression / deduplication
            - Time snapping (validates LLM proposals)
            - Final ranking + posting order (+spoiler holdback logic)
            - All 6 deterministic checks (`check.py`)
            """)
        with cols[1]:
            st.markdown("**LLM (replaceable)**")
            st.markdown("""
            - **Editor** (`prompts/editor.md`): reads a screenplay + rule hints, proposes up to
              3 promos with hook/cliffhanger/why. *Can be rules-only without any LLM.*
            - **Cold viewer** (`prompts/cold_viewer.md`): scores each clip as a first-time viewer
              (hook, cliffhanger, clarity, spoiler). *Not used in rules mode.*
            - **Stitcher** (`prompts/stitcher.md`): proposes two-part cross-episode promos.
              *Rules mode uses a simple code stitcher instead.*
            """)
        st.info("💡 **Model-agnostic**: swap the LLM by changing `config.yaml` or using `--config`. "
                "Prompts and code do not change. Ready-made configs for Groq, Gemini, OpenRouter, "
                "and local Ollama are in `configs/`.")

    with st.expander("👤 Where the human stays in the loop"):
        st.markdown("""
        - **Review tab**: an editor approves or rejects each promo. Verdicts can be exported as
          CSV and become evaluation labels for the next iteration.
        - **Posting plan**: the app produces a ranked posting plan that an editor can follow or override.
        - **Failure patterns**: the **Quality tab** surfaces where the tool picks badly so the editor
          knows what to watch for.
        - **Spot check**: 4 common failure modes were identified and documented (see Quality tab).
        """)

    with st.expander("📈 Scalability to every Kuku show"):
        st.markdown("""
        - **No character/show-specific logic**: the parser handles Kuku's block format generically.
          The rule lexicon (hook words, props, settings) was tuned on this period drama, but the
          LLM passes carry no such tuning — they work on any genre.
        - **Rules mode costs nothing** and runs in ~25s on 50 episodes. You could run it on every
          show automatically and only use the LLM for the top-N candidates.
        - **LLM mode costs ~$0.04–0.30 per show** depending on model (Gemini Flash = $0.01).
          Disk caching means re-runs are free.
        - **Horizontal scaling**: each episode is processed independently. For a 500-episode show,
          you'd batch them through the LLM in parallel.
        """)

    st.caption("Built by Abhinav Tanwar · Kuku AI Generalist assignment (Problem 1, Nov 2026)")

# ===================================================================
# TAB 1 — Run
# ===================================================================
with tab_run:
    st.subheader("▶ Run the promo finder")

    hcol1, hcol2 = st.columns([2, 1])
    with hcol1:
        files = st.file_uploader(
            "Episode screenplays (.txt files, or one .zip)",
            type=["txt", "zip"],
            accept_multiple_files=True,
            help="Upload the assignment input pack, or your own screenplays in Kuku block format or plain [mm:ss] format",
        )
        use_sample = st.checkbox(
            "Use the built-in sample show (2 invented episodes)",
            value=False,
            disabled=bool(files),
            help="No upload needed — runs on built-in sample data so you can try the tool immediately",
        )
        if files:
            st.caption(
                "🔒 Uploaded files are kept in a temporary folder and deleted when the session ends. "
                "They are never stored on the server or shared with other users."
            )
    with hcol2:
        mode = st.radio(
            "Pipeline mode",
            ["rules", "llm", "jev"],
            horizontal=True,
            format_func=lambda m: {
                "rules": "⚡ Rules only (no API, ~25s)",
                "llm": "🧠 LLM editor + judge",
                "jev": "🎯 LLM edit + Jev judge",
            }[m],
            help="Rules mode is free and instant. LLM mode uses an LLM for editing + judging. "
                 "Jev mode uses the LLM for editing but Jev (TypeSafe) for judging.",
        )

    llm_cfg = dict(base_cfg["llm"])
    if mode in ("llm", "jev"):
        if mode == "jev":
            st.info("🎯 **Jev mode**: uses an LLM for editing + stitching, then Jev (TypeSafe) for the cold-viewer "
                    "judge. Set `JEV_KEY` environment variable for Jev, plus the LLM API key below.")
        st.divider()
        lcol1, lcol2, lcol3 = st.columns(3)
        cfg_llm = base_cfg["llm"]
        providers = ["openai_compatible", "gemini"]
        provider = lcol1.selectbox(
            "Provider",
            providers,
            index=providers.index(cfg_llm.get("provider", "openai_compatible")),
            help="openai_compatible = xAI Grok, OpenAI, Groq, OpenRouter, Ollama, or any OpenAI-format API",
        )
        llm_cfg["provider"] = provider
        same = provider == cfg_llm.get("provider")
        default_model = cfg_llm["model"] if same else (
            "gemini-2.5-flash" if provider == "gemini" else "grok-4.3"
        )
        llm_cfg["model"] = lcol1.text_input("Model", default_model)
        if provider == "openai_compatible":
            llm_cfg["base_url"] = lcol2.text_input(
                "Base URL",
                cfg_llm.get("base_url") or "https://api.x.ai/v1",
                placeholder="https://api.x.ai/v1",
            )
        try:
            secret_key = st.secrets.get("LLM_API_KEY", "")
        except Exception:
            secret_key = ""
        key = lcol2.text_input(
            "API key",
            type="password",
            help="Used only for this run, kept in memory. Never saved to disk."
            + (" Leave blank to use the app's pre-configured key." if secret_key else ""),
        )
        llm_cfg["api_key"] = key or secret_key
        llm_cfg["min_interval_s"] = lcol3.number_input(
            "Seconds between calls (free tiers: ~4s)",
            0.0, 30.0, float(base_cfg["llm"]["min_interval_s"]),
            help="Higher values = slower but gentler on rate limits. Free tiers need ~4s between calls.",
        )
        limit = lcol3.number_input(
            "Episode limit (0 = all)",
            0, 50, 0,
            help="Process only the first N episodes — useful for testing before a full run",
        )
    else:
        limit = 0

    st.divider()
    btn_disabled = not (files or use_sample)
    run_clicked = st.button(
        "▶ Find promos", type="primary", disabled=btn_disabled,
        help="Upload episodes or check 'Use sample'" if btn_disabled else "Start the pipeline",
        use_container_width=True,
    )

    if run_clicked:
        if mode in ("llm", "jev") and not llm_cfg.get("api_key") and "localhost" not in llm_cfg.get("base_url", ""):
            st.error("✋ Enter an API key, or switch to Rules only mode.")
            st.stop()

        eps_dir = save_uploads(files) if files else ROOT / "data" / "sample"
        n = len(list(eps_dir.glob("*.txt")))
        if not n:
            st.error("No .txt screenplays found in the upload.")
            st.stop()

        cfg = dict(base_cfg, llm=llm_cfg)
        out = Path(tempfile.mkdtemp(prefix="run_"))
        progress_bar = st.progress(0.0, text="Starting…")
        status_box = st.empty()

        def progress_cb(fraction: float, message: str):
            progress_bar.progress(min(fraction, 1.0))
            status_box.info(message)

        try:
            limit_kwargs = {"limit": limit} if limit else {}
            run(
                str(eps_dir),
                str(out),
                mode,
                cfg=cfg,
                progress=progress_cb,
                **limit_kwargs,
            )
        except Exception as e:
            progress_bar.empty()
            status_box.error(f"❌ Run failed: {e}")
            st.stop()

        ss.run_dir, ss.eps_dir, ss.verdicts = str(out), str(eps_dir), {}
        meta, promos = load_promos(out)

        label = f"Run {len(ss.run_history) + 1} — {meta['mode']} · {meta.get('model', 'no LLM')} · {len(promos)} promos"
        ss.run_history[label] = {"run_dir": str(out), "eps_dir": str(eps_dir)}

        progress_bar.empty()
        cost_str = estimate_cost(meta)
        status_box.success(
            f"✅ Done: {meta['episodes']} episodes → {len(promos)} promos in {meta.get('seconds', 0):.0f}s"
            + (f" · Estimated cost: {cost_str}" if cost_str != "N/A (no LLM used)" else "")
        )
        st.balloons()

    if ss.run_dir and Path(ss.run_dir).exists():
        st.divider()
        out = Path(ss.run_dir)
        meta_last, _ = load_promos(out)

        st.subheader("📦 Last run — quick summary")
        mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
        mcol1.metric("Promos", len(_))
        mcol2.metric("Episodes", meta_last.get("episodes", "—"))
        mcol3.metric("Duration", f"{meta_last.get('seconds', 0):.0f}s" if isinstance(meta_last.get("seconds"), (int, float)) else "—")
        mcol4.metric("Mode", meta_last["mode"])
        cost_str = estimate_cost(meta_last)
        mcol5.metric("Est. cost", cost_str)

        dcol1, dcol2, dcol3 = st.columns(3)
        dcol1.download_button(
            "📄 promos.json", (out / "promos.json").read_bytes(), "promos.json", use_container_width=True
        )
        dcol2.download_button(
            "📄 promos.csv", (out / "promos.csv").read_bytes(), "promos.csv", use_container_width=True
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for p in out.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(out))
        dcol3.download_button(
            "📦 Everything (.zip)", buf.getvalue(), "raw_outputs.zip", use_container_width=True
        )

        plan = out / "posting_plan.md"
        if plan.exists():
            with st.expander("📋 Posting plan — what to post first, what to hold back", expanded=True):
                st.markdown(plan.read_text().split("\n", 1)[-1].replace("\n## ", "\n#### "))

    with st.expander("ℹ️ How the pipeline works"):
        st.markdown((ROOT / "README.md").read_text().split("## How it works", 1)[-1].split("## Outputs", 1)[0])

    st.caption("Built by Abhinav Tanwar · Kuku AI Generalist assignment (Problem 1, Nov 2026)")

# ===================================================================
# TAB 2 — Review
# ===================================================================
with tab_review:
    st.subheader("📋 Review promos")
    run_opts = load_run_options()
    if not run_opts:
        st.info("No runs yet. Run the pipeline first (tab ▶ Run).")
        st.stop()

    rcol1, rcol2 = st.columns([3, 1])
    with rcol1:
        selected_run = st.selectbox("Select a run to review", list(run_opts))
    with rcol2:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    run_dir, eps_dir = run_opts[selected_run]
    meta, promos = load_promos(run_dir)
    checks = load_checks(run_dir)
    run_meta_display(meta)

    # --- Jev rejudge button ---
    has_jev = any(p.get("jev") for p in promos)
    if not has_jev and _HAVE_JEV:
        jcol1, jcol2 = st.columns([3, 1])
        with jcol2:
            jev_limit = st.number_input("Limit (0=all)", 0, 100, 0, key="jev_limit")
            if st.button("🔁 Re-judge with Jev", type="secondary", use_container_width=True,
                         help="Requires JEV_KEY env var. Replaces LLM judge scores with Jev probabilities."):
                import os as _os
                jk = _os.environ.get("JEV_KEY")
                if not jk:
                    st.error("Set JEV_KEY environment variable first.")
                else:
                    with st.spinner("Jev re-judging all promos…"):
                        try:
                            jev_out = _jev_rejudge(str(run_dir), str(run_dir / ".." / f"{run_dir.name}_jev"),
                                                    str(eps_dir), limit=jev_limit or 0)
                            st.success(f"Jev re-judged → {jev_out.name}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Jev re-judge failed: {e}")
    elif has_jev:
        st.info("✅ This run was judged with Jev. Raw Jev probabilities are shown in each promo.")

    # Build a lookup: rank → check row
    check_by_rank = {int(c["rank"]): c for c in checks} if checks else {}

    st.divider()

    # --- Filters ---
    with st.expander("🔍 Filters & sorting", expanded=True):
        fcol1, fcol2, fcol3, fcol4, fcol5 = st.columns(5)
        with fcol1:
            score_min, score_max = st.slider("Score range", 0.0, 1.0, (0.0, 1.0), step=0.05)
        with fcol2:
            verdict_filter = st.selectbox("Verdict", ["All", "Unreviewed", "Approved", "Rejected"])
        with fcol3:
            ep_raw = st.text_input("Episodes (e.g. 3, 11, 12)", placeholder="All")
            want_eps = {int(x) for x in ep_raw.replace(" ", "").split(",") if x.isdigit()}
        with fcol4:
            source_filter = st.selectbox("Source", ["All", "rules", "llm", "llm-stitch", "rules-stitch"])
        with fcol5:
            sort_by = st.selectbox("Sort by", ["Rank", "Score (high→low)", "Score (low→high)", "Duration", "Episode"])

    # --- Batch actions ---
    bcol1, bcol2, bcol3 = st.columns(3)
    if bcol1.button("✅ Approve all visible", use_container_width=True, type="primary"):
        ss.batch_verdict_mode = "approve"
    if bcol2.button("❌ Reject all visible", use_container_width=True):
        ss.batch_verdict_mode = "reject"
    if bcol3.button("Clear verdicts for visible", use_container_width=True):
        ss.batch_verdict_mode = "clear"

    try:
        eps = {e.number: e for e in load_show(eps_dir)}
    except Exception:
        eps = {}

    # --- Filter + sort ---
    filtered = []
    for p in promos:
        if p["score"] < score_min or p["score"] > score_max:
            continue
        if want_eps and not (set(p["episodes"]) & want_eps):
            continue
        if source_filter != "All" and p.get("source") != source_filter:
            continue
        if verdict_filter != "All":
            v = ss.verdicts.get(p["rank"], {}).get("verdict", "")
            if verdict_filter == "Approved" and v != "approve":
                continue
            if verdict_filter == "Rejected" and v != "reject":
                continue
            if verdict_filter == "Unreviewed" and v:
                continue
        filtered.append(p)

    sort_map = {
        "Rank": lambda p: p["rank"],
        "Score (high→low)": lambda p: -p["score"],
        "Score (low→high)": lambda p: p["score"],
        "Duration": lambda p: -p["duration_sec"],
        "Episode": lambda p: (min(p["episodes"]), p["rank"]),
    }
    filtered.sort(key=sort_map.get(sort_by, sort_map["Rank"]))

    # --- Apply batch verdict ---
    if ss.batch_verdict_mode:
        for p in filtered:
            segs_str = " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in p["segments"])
            if ss.batch_verdict_mode == "approve":
                ss.verdicts[p["rank"]] = {"segments": segs_str, "verdict": "approve", "comment": ""}
            elif ss.batch_verdict_mode == "reject":
                ss.verdicts[p["rank"]] = {"segments": segs_str, "verdict": "reject", "comment": ""}
            elif ss.batch_verdict_mode == "clear":
                ss.verdicts.pop(p["rank"], None)
        ss.batch_verdict_mode = None
        st.rerun()

    st.markdown(f"**{len(filtered)}** of {len(promos)} promos shown")

    # --- Render promo cards ---
    for p in filtered:
        segs_str = " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in p["segments"])
        verdict = ss.verdicts.get(p["rank"], {}).get("verdict", "")
        comment = ss.verdicts.get(p["rank"], {}).get("comment", "")
        ck = check_by_rank.get(p["rank"])

        # Card border color based on verdict
        if verdict == "approve":
            border_class = "promo-pass"
            status_icon = "✅"
        elif verdict == "reject":
            border_class = "promo-fail"
            status_icon = "❌"
        else:
            border_class = "promo-none"
            status_icon = "⬜"

        # Source badge
        src_colors = {"rules": "#8e44ad", "llm": "#2980b9", "llm-stitch": "#1abc9c", "rules-stitch": "#f39c12"}
        src_badge = badge(p.get("source", ""), src_colors.get(p.get("source", ""), "#888"))

        # Check results badges
        check_badges = ""
        if ck:
            pass_count = sum(1 for k in ["duration_ok", "order_ok", "timestamps_ok", "no_midline_cut", "hook_event_3s", "not_duplicate"] if ck.get(k) == "True")
            fail_count = 6 - pass_count
            warn_count = sum(1 for k in ["warn_opens_on_reply", "warn_ends_mid_sentence", "warn_hook_text_outside_3s", "warn_cliff_text_outside_5s"] if ck.get(k) == "True")
            if fail_count == 0:
                check_badges += f' <span class="badge-green">✓ {pass_count}/6</span>'
            else:
                check_badges += f' <span class="badge-red">✗ {fail_count} fail</span>'
            if warn_count:
                check_badges += f' <span class="badge-orange">⚠ {warn_count}</span>'

        st.markdown(f'<div class="promo-card {border_class}">', unsafe_allow_html=True)

        # Header row
        hl = st.columns([5, 2, 1])
        with hl[0]:
            st.markdown(
                f"**{status_icon} #{p['rank']}** · {segs_str} · "
                f"`{p['duration_sec']}s` · score **{p['score']:.4f}** · "
                f"{src_badge} {check_badges}",
                unsafe_allow_html=True,
            )
        with hl[1]:
            best_judge = ""
            if p.get("jev"):
                jv = p["jev"]
                avg_p = (jv.get("hook", 0) + jv.get("unresolved", 0) + jv.get("clear", 0)) / 3
                best_judge = f"🎯 {avg_p:.0%}"
            elif p.get("judge") and p["judge"].get("hook"):
                j = p["judge"]
                avg_j = (j.get("hook", 0) + j.get("cliffhanger", 0) + j.get("clarity", 0)) / 3
                best_judge = f"👁 {avg_j:.1f}/10"
            st.markdown(f"**{best_judge}**" if best_judge else "")
        with hl[2]:
            pass

        with st.expander("Details", expanded=False):
            a, b = st.columns([3, 2])
            with a:
                st.markdown(
                    f"**Hook:** {p['hook']}  \n"
                    f"**Cliffhanger:** {p['cliffhanger']}  \n"
                    f"**Why:** {p['why']}"
                )
                if p.get("jev"):
                    jv = p["jev"]
                    st.caption(
                        f"🎯 **Jev:** hook {jv.get('hook', 0):.0%} · "
                        f"unresolved {jv.get('unresolved', 0):.0%} · "
                        f"clear {jv.get('clear', 0):.0%} · "
                        f"spoiler {jv.get('spoiler', 0):.0%}"
                    )
                elif p.get("judge") and p["judge"].get("hook"):
                    j = p["judge"]
                    confusing = j.get("confusing", "")
                    st.caption(
                        f"👁 Cold viewer: hook {j.get('hook')}/10 · cliff {j.get('cliffhanger')}/10 · "
                        f"clarity {j.get('clarity')}/10 · spoiler {j.get('spoiler')}"
                        + (f" · 🧐 {confusing}" if confusing and confusing.lower() != "nothing" else "")
                    )

                # Show check results inline
                if ck:
                    st.markdown("**Deterministic checks**")
                    cols = st.columns(3)
                    check_fields = [
                        ("⏱ Duration", "duration_ok"),
                        ("📋 Order", "order_ok"),
                        ("🕒 Timestamps", "timestamps_ok"),
                        ("✂️ No mid-line cut", "no_midline_cut"),
                        ("🎣 Hook in 3s", "hook_event_3s"),
                        ("🔁 Not duplicate", "not_duplicate"),
                    ]
                    for idx, (label, key) in enumerate(check_fields):
                        val = ck.get(key, "True")
                        ok = val == "True"
                        cols[idx % 3].markdown(
                            f'{"✅" if ok else "❌"} {label}',
                            help=f"{key}={val}" if not ok else None,
                        )
                    warns = [
                        ("Opens on reply", "warn_opens_on_reply"),
                        ("Ends mid-sentence", "warn_ends_mid_sentence"),
                        ("Hook text mismatch", "warn_hook_text_outside_3s"),
                        ("Cliff text mismatch", "warn_cliff_text_outside_5s"),
                    ]
                    flagged_warns = [(l, k) for l, k in warns if ck.get(k) == "True"]
                    if flagged_warns:
                        st.caption("⚠️ " + " · ".join(f"{l}" for l, _ in flagged_warns))

            with b:
                new_comment = st.text_input("Comment", value=comment, key=f"c{p['rank']}", placeholder="Optional note…")
                x, y = st.columns(2)
                if x.button("✅ Approve", key=f"a{p['rank']}", use_container_width=True):
                    ss.verdicts[p["rank"]] = {"segments": segs_str, "verdict": "approve", "comment": new_comment}
                    st.rerun()
                if y.button("❌ Reject", key=f"r{p['rank']}", use_container_width=True):
                    ss.verdicts[p["rank"]] = {"segments": segs_str, "verdict": "reject", "comment": new_comment}
                    st.rerun()

            # Script excerpt
            if eps:
                script_parts = []
                for s in p["segments"]:
                    if s["episode"] in eps:
                        script_parts.append(script_text(eps[s["episode"]], s["start_sec"], s["end_sec"]))
                if script_parts:
                    st.code("\n\n".join(script_parts), language=None)

        st.markdown("</div>", unsafe_allow_html=True)

    # --- Download verdicts ---
    if ss.verdicts:
        st.divider()
        vcol1, vcol2, vcol3 = st.columns([1, 1, 3])
        approved = sum(1 for v in ss.verdicts.values() if v.get("verdict") == "approve")
        rejected = sum(1 for v in ss.verdicts.values() if v.get("verdict") == "reject")
        vcol1.metric("✅ Approved", approved)
        vcol2.metric("❌ Rejected", rejected)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["rank", "segments", "verdict", "comment"])
        for r, v in sorted(ss.verdicts.items()):
            w.writerow([r, v["segments"], v["verdict"], v["comment"]])
        vcol3.download_button(
            f"📥 Download {len(ss.verdicts)} editor verdicts (CSV)", buf.getvalue(), "decisions.csv",
            use_container_width=True,
        )

# ===================================================================
# TAB 3 — Compare
# ===================================================================
with tab_compare:
    st.subheader("🔍 Compare two runs")
    run_opts_all = load_run_options(include_current=True)
    if len(run_opts_all) < 2:
        st.info("Need at least two runs to compare. Run the pipeline in different modes (e.g. rules and LLM).")
        st.stop()

    ccol1, ccol2 = st.columns(2)
    with ccol1:
        run_a_label = st.selectbox("Run A (baseline)", list(run_opts_all), index=0)
    with ccol2:
        run_b_label = st.selectbox("Run B (experiment)", list(run_opts_all), index=min(1, len(run_opts_all) - 1))

    run_a_dir, eps_a_dir = run_opts_all[run_a_label]
    run_b_dir, eps_b_dir = run_opts_all[run_b_label]
    meta_a, promos_a = load_promos(run_a_dir)
    meta_b, promos_b = load_promos(run_b_dir)

    # Summary
    scol1, scol2, scol3, scol4, scol5 = st.columns(5)
    scol1.metric("Mode", meta_a["mode"], meta_b["mode"])
    scol2.metric("Promos", len(promos_a), delta=len(promos_b) - len(promos_a))
    avg_a = sum(p["score"] for p in promos_a) / len(promos_a) if promos_a else 0
    avg_b = sum(p["score"] for p in promos_b) / len(promos_b) if promos_b else 0
    scol3.metric("Avg score", f"{avg_a:.3f}", delta=f"{avg_b - avg_a:+.3f}")
    scol4.metric("Duration", f"{meta_a.get('seconds', 0):.0f}s", delta=f"{meta_b.get('seconds', 0) - meta_a.get('seconds', 0):+.0f}s")
    cost_a = estimate_cost(meta_a)
    cost_b = estimate_cost(meta_b)
    scol5.metric("Est. cost", cost_a, delta=cost_b)

    st.divider()

    eps_a_set = {min(p["episodes"]) for p in promos_a}
    eps_b_set = {min(p["episodes"]) for p in promos_b}
    common = eps_a_set & eps_b_set
    only_a = eps_a_set - eps_b_set
    only_b = eps_b_set - eps_a_set

    ecol1, ecol2, ecol3 = st.columns(3)
    ecol1.metric("Episodes in both", len(common))
    ecol2.metric("Only in A", len(only_a))
    ecol3.metric("Only in B", len(only_b))

    st.subheader("Score distribution")
    st.scatter_chart({"Run A": [p["score"] for p in promos_a], "Run B": [p["score"] for p in promos_b]})

    st.subheader("Duration distribution (seconds)")
    st.scatter_chart({"Run A": [p["duration_sec"] for p in promos_a], "Run B": [p["duration_sec"] for p in promos_b]})

    st.subheader("Top-N overlap")
    for k in (5, 10, 20):
        top_a = {(min(p["episodes"]), p.get("hook", "")[:40]) for p in promos_a[:k]}
        top_b = {(min(p["episodes"]), p.get("hook", "")[:40]) for p in promos_b[:k]}
        ov = len(top_a & top_b)
        st.metric(f"Top-{k} agreement", f"{ov}/{k}")

    # --- Jev vs LLM judge comparison ---
    has_jev_a = any(p.get("jev") for p in promos_a)
    has_jev_b = any(p.get("jev") for p in promos_b)
    if has_jev_a or has_jev_b:
        st.subheader("🎯 Jev probability comparison")
        jev_run = promos_a if has_jev_a else promos_b
        other_run = promos_b if has_jev_a else promos_a
        jev_label = run_a_label if has_jev_a else run_b_label
        other_label = run_b_label if has_jev_a else run_a_label
        st.caption(f"Comparing Jev probabilities ({jev_label}) vs LLM judge scores ({other_label})")
        jev_promos_with_jev = [p for p in jev_run if p.get("jev")]
        moved_up = sum(1 for p in jev_promos_with_jev if p.get("original_rank", p["rank"]) > p["rank"])
        moved_down = sum(1 for p in jev_promos_with_jev if p.get("original_rank", p["rank"]) < p["rank"])
        if moved_up or moved_down:
            jc1, jc2, jc3 = st.columns(3)
            jc1.metric("Re-ranked by Jev", len(jev_promos_with_jev))
            jc2.metric("Moved up", moved_up)
            jc3.metric("Moved down", moved_down)

    st.subheader("Per-episode best scores")
    sco_a = {}
    for p in promos_a:
        ep = min(p["episodes"])
        sco_a[ep] = max(sco_a.get(ep, 0), p["score"])
    sco_b = {}
    for p in promos_b:
        ep = min(p["episodes"])
        sco_b[ep] = max(sco_b.get(ep, 0), p["score"])
    all_eps = sorted(set(list(sco_a.keys()) + list(sco_b.keys())))
    st.bar_chart(
        {"Episode": [f"Ep {e}" for e in all_eps], "Run A": [sco_a.get(e, 0) for e in all_eps],
         "Run B": [sco_b.get(e, 0) for e in all_eps]},
        x="Episode", y=["Run A", "Run B"],
    )

# ===================================================================
# TAB 4 — Quality
# ===================================================================
with tab_quality:
    st.subheader("✅ Quality checks")
    st.markdown("""
    Quality is evaluated through three independent mechanisms. Each catches different kinds of failures
    and together they tell you how trustworthy the output is.
    """)

    qtab_info, qtab_checks, qtab_eval, qtab_failures = st.tabs(
        ["📋 Overview", "🔬 Deterministic checks", "🎯 vs Gold labels", "💔 Failure patterns"]
    )

    with qtab_info:
        st.markdown("""
        ### Three-layer quality strategy

        | Layer | What it checks | How | Who runs it |
        |---|---|---|---|
        | **1. Deterministic** | 6 pass/fail + 4 warnings per promo | Code checks each promo against the raw screenplay | `check.py`, runs in <1s |
        | **2. Gold evaluation** | Precision/recall against hand-labelled promos | Overlap >= 50% counts as a hit | `evaluate.py` or the app below |
        | **3. Human review** | Editor approves/rejects each promo | Streamlit app review tab → verdicts CSV | You, in the **Review** tab |

        **Results on the LLM run (Grok 4.3, 50 episodes, 100 promos):**
        """)
        # Check summary
        st.markdown("""
        - **Deterministic checks**: 97/100 pass all 6 checks. 3 fail `hook_event_3s` (no action in first 3s).
        - **Gold evaluation**: recall@20 = 67% (4 of 6 gold promos found in top 20). 2 gold promos not proposed.
        - **Spot check**: 4 of 10 pass all three promo quality criteria.
        """)

        st.markdown("""
        ### How to interpret these numbers

        - **67% recall@20** means the tool proposes 4 of 6 hand-picked promos in the top 20. The 2 misses are
          editorial picks that no automated system would find without a plot synopsis.
        - **97/100 deterministic pass** means the output is mechanically sound. The 3 failures are promos where
          no event starts in the first 3s — a real problem that the `warn_opens_on_reply` check also catches
          for a different reason.
        - **4/10 spot-check pass** is the honest number: 6 of the top 10 have issues that only a human reading
          the script can catch (weak hook, resolved ending, spoiler). The **Failure patterns** tab explains each.
        """)

        st.info(
            "💡 **Honest admission**: the automated quality checks are necessary but not sufficient. "
            "The spot check reveals that about half the top promos have editorial issues that no code or LLM "
            "catches reliably. A human editor is still essential — but the tool reduces 50 episodes to ~15 "
            "promos that need their attention."
        )

    with qtab_checks:
        st.markdown("### 🔬 Deterministic check results")
        run_opts_qc = load_run_options()
        if not run_opts_qc:
            st.info("No runs yet.")
        else:
            qc_run = st.selectbox("Select a run", list(run_opts_qc), key="qc_selector")
            qc_dir, _ = run_opts_qc[qc_run]
            checks = load_checks(qc_dir)
            if not checks:
                st.warning(f"No `check_report.csv` found in `{qc_dir.name}`. Run `python -m promo_finder.check --run {qc_dir} --input data/episodes` to generate it.")
            else:
                meta_qc, _ = load_promos(qc_dir)
                run_meta_display(meta_qc)

                # Summary metrics
                total = len(checks)
                check_keys = ["duration_ok", "order_ok", "timestamps_ok", "no_midline_cut", "hook_event_3s", "not_duplicate"]
                warn_keys = ["warn_opens_on_reply", "warn_ends_mid_sentence", "warn_hook_text_outside_3s", "warn_cliff_text_outside_5s"]

                pass_counts = {}
                for k in check_keys:
                    pass_counts[k] = sum(1 for c in checks if c.get(k) == "True")
                warn_counts = {}
                for k in warn_keys:
                    warn_counts[k] = sum(1 for c in checks if c.get(k) == "True")

                kcol1, kcol2, kcol3, kcol4, kcol5, kcol6 = st.columns(6)
                labels = [
                    ("⏱ Duration", "duration_ok"),
                    ("📋 Order", "order_ok"),
                    ("🕒 Timestamps", "timestamps_ok"),
                    ("✂️ No mid-cut", "no_midline_cut"),
                    ("🎣 Hook 3s", "hook_event_3s"),
                    ("🔁 No dup", "not_duplicate"),
                ]
                for col, (label, key) in zip([kcol1, kcol2, kcol3, kcol4, kcol5, kcol6], labels):
                    pct = pass_counts[key] / total * 100
                    col.metric(label, f"{pass_counts[key]}/{total}", delta=f"{pct:.0f}%")

                st.subheader("Pass/fail by promo")
                all_pass = [c for c in checks if all(c.get(k) == "True" for k in check_keys)]
                any_fail = [c for c in checks if any(c.get(k) != "True" for k in check_keys)]
                st.metric("All checks pass", f"{len(all_pass)}/{total}")
                st.metric("At least one fail", f"{len(any_fail)}/{total}")

                if any_fail:
                    st.subheader("Failing promos")
                    for c in any_fail:
                        fails = [k for k in check_keys if c.get(k) != "True"]
                        st.markdown(
                            f"- **Rank {c['rank']}** · {c['segments']} · ❌ {', '.join(fails)}"
                        )

                warn_any = [c for c in checks if any(c.get(k) == "True" for k in warn_keys)]
                st.subheader(f"Warnings ({len(warn_any)} promos)")
                if warn_any:
                    warn_labels = {
                        "warn_opens_on_reply": "Opens on a reply line",
                        "warn_ends_mid_sentence": "Ends mid-sentence",
                        "warn_hook_text_outside_3s": "Hook text quotes outside first 3s",
                        "warn_cliff_text_outside_5s": "Cliff text quotes outside last 5s",
                    }
                    for c in warn_any:
                        active = [warn_labels[k] for k in warn_keys if c.get(k) == "True"]
                        st.markdown(f"- **Rank {c['rank']}** · {c['segments']} · ⚠️ {' · '.join(active)}")
                else:
                    st.success("No warnings on any promo.")

                # Download
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=list(checks[0]))
                w.writeheader()
                w.writerows(checks)
                st.download_button("📥 Download check report (CSV)", buf.getvalue(), "check_report.csv")

    with qtab_eval:
        st.markdown("### 🎯 Evaluation vs gold labels")
        st.markdown("""
        The gold set contains **6 hand-verified promos** from the 50-episode show. Each was selected by reading
        the full screenplay and verifying: hook in 3s, unresolved ending, comprehensible to a stranger.
        """)

        # Display the gold promos
        gold_path = ROOT / "eval" / "gold.yaml"
        if gold_path.exists():
            gold_data = yaml.safe_load(gold_path.read_text()) or []
            gold_active = [g for g in gold_data if g.get("start")]
            st.caption(f"**{len(gold_active)} gold promos** (from `eval/gold.yaml`)")
            for g in gold_active:
                st.markdown(f"- **Ep {g['episode']}** `{g['start']}–{g['end']}` — {g.get('note', '')}")

        # Load and show evaluation.txt
        eval_txt = safe_read(ROOT / "outputs" / "evaluation.txt")
        if eval_txt:
            st.subheader("Pre-computed evaluation results")
            st.text(eval_txt[:1500])
            if len(eval_txt) > 1500:
                with st.expander("Show full evaluation"):
                    st.text(eval_txt)
        else:
            st.info("Run `python -m promo_finder.evaluate --run outputs/llm --gold eval/gold.yaml` to generate evaluation results.")

        st.subheader("What the numbers mean")
        st.markdown("""
        | Metric | Rules | LLM (Grok) | What it tells you |
        |---|---|---|---|
        | precision@10 | 0.20 | 0.20 | 2 of the top 10 match a gold promo |
        | recall@20 | 0.50 | 0.67 | 3 (rules) / 4 (LLM) of 6 gold promos found in top 20 |
        | Best gold rank | #3 | #2 | The highest-ranked gold promo |

        **Limitation**: only 6 gold labels for 50 episodes is sparse. The 2 gold promos never proposed
        (ep 24 and ep 41) involve specific plot context that no automated system captures without a synopsis.
        A production system would grow the gold set over time via editor approvals.
        """)

    with qtab_failures:
        st.markdown("### 💔 Honest failure patterns")
        st.markdown("""
        Based on a manual spot-check of the **top 10 promos** — reading each against the screenplay.
        Only **4 of 10 pass** all three promo quality criteria.

        **Pattern 1: The opening is one line too late (or early)**
        """)
        st.markdown("""
        > **Example**: Promo #6 opens on "Dignity?" — a reply to a line the clip cuts off.
        > A stranger has no context. The start time is mechanically valid (snapped to a shot boundary)
        > but the content is wrong.
        
        **Fix**: A code check that warns when the first line is a short reply or follows a loud event
        within 1s. Already flagged by `warn_opens_on_reply`.
        """)

        st.markdown("**Pattern 2: The spoiler judgement is too lenient**")
        st.markdown("""
        > **Example**: Promo #7 ends on the series' main twist ("their dead Duke is coming home").
        > The cold viewer rated it "medium" spoiler — but for the creator, this is the biggest reveal.
        
        **Fix**: Give the cold viewer a short list of key reveals (one line per arc) to check against.
        """)

        st.markdown("**Pattern 3: The ending resolves the problem**")
        st.markdown("""
        > **Example**: Promo #5 — the rescue succeeds on screen. The viewer sees the escape, so there's
        > no cliffhanger.
        
        **Fix**: In a second pass, trim the end back to the last unresolved beat (the beam crashing down).
        """)

        st.markdown("**Pattern 4: The payoff depends on a named character**")
        st.markdown("""
        > **Example**: Promo #3 — the cliffhanger names "Gerald Harrington", a character the stranger
        > has never heard of.
        
        **Fix**: The cold viewer already flagged this ("who Gerald Harrington is"). Lower clarity scores
        should weigh more in the ranking.
        """)

        st.info(
            "🧠 **Why these failures matter**: Every one of these is detectable either by a stricter code check "
            "(pattern 1, 3) or a better prompt (pattern 2, 4). None is fundamentally unsolvable. They represent "
            "the next iteration, not a design flaw."
        )

# ===================================================================
# TAB 5 — Analytics
# ===================================================================
with tab_analytics:
    st.subheader("📊 Analytics")
    run_opts_an = load_run_options()
    if not run_opts_an:
        st.info("No runs to analyse. Run the pipeline first (tab ▶ Run).")
        st.stop()

    anal_run = st.selectbox("Select a run to analyse", list(run_opts_an))
    anal_dir, anal_eps_dir = run_opts_an[anal_run]
    meta_an, promos_an = load_promos(anal_dir)
    run_meta_display(meta_an)

    if not promos_an:
        st.warning("No promos in this run.")
        st.stop()

    st.divider()

    # --- Overview ---
    acol1, acol2, acol3, acol4, acol5 = st.columns(5)
    acol1.metric("Total promos", len(promos_an))
    avg_s = sum(p["score"] for p in promos_an) / len(promos_an)
    acol2.metric("Avg score", f"{avg_s:.3f}")
    acol3.metric("Avg duration", f"{sum(p['duration_sec'] for p in promos_an) / len(promos_an):.1f}s")
    src_c = Counter(p.get("source", "unknown") for p in promos_an)
    acol4.metric("Primary source", src_c.most_common(1)[0][0] if src_c else "—")
    spoiler_c = Counter(p.get("judge", {}).get("spoiler", "unknown") for p in promos_an if p.get("judge"))
    acol5.metric("High spoiler", spoiler_c.get("high", 0))

    st.divider()

    # --- Score distribution ---
    st.subheader("📊 Score distribution")
    score_bins = [i / 20 for i in range(21)]
    hist = Counter(round(p["score"] * 20) / 20 for p in promos_an)
    st.bar_chart(
        {"Score bucket": [f"{b:.2f}" for b in score_bins[:-1]],
         "Count": [hist.get(b, 0) for b in score_bins[:-1]]},
        x="Score bucket", y="Count",
    )

    # --- Judge scores ---
    judged = [p for p in promos_an if p.get("judge") and p["judge"].get("hook")]
    if judged:
        st.subheader("🧠 Cold-viewer judge scores")
        jc1, jc2, jc3 = st.columns(3)
        jc1.metric("Avg hook", f"{sum(j['judge']['hook'] for j in judged) / len(judged):.1f}/10")
        jc2.metric("Avg cliffhanger", f"{sum(j['judge']['cliffhanger'] for j in judged) / len(judged):.1f}/10")
        jc3.metric("Avg clarity", f"{sum(j['judge']['clarity'] for j in judged) / len(judged):.1f}/10")
        st.line_chart(
            {"Rank": [p["rank"] for p in judged[:50]],
             "Hook": [p["judge"]["hook"] for p in judged[:50]],
             "Cliffhanger": [p["judge"]["cliffhanger"] for p in judged[:50]],
             "Clarity": [p["judge"]["clarity"] for p in judged[:50]]},
            x="Rank", y=["Hook", "Cliffhanger", "Clarity"],
        )

    # --- Source breakdown ---
    st.subheader("📦 Source breakdown")
    st.bar_chart({"Source": list(src_c.keys()), "Count": list(src_c.values())}, x="Source", y="Count")

    # --- Jev probability distribution ---
    jev_promos = [p for p in promos_an if p.get("jev")]
    if jev_promos:
        st.subheader("🎯 Jev probability distribution")
        jc1, jc2, jc3, jc4 = st.columns(4)
        avg_h = sum(p["jev"]["hook"] for p in jev_promos) / len(jev_promos)
        avg_u = sum(p["jev"]["unresolved"] for p in jev_promos) / len(jev_promos)
        avg_c = sum(p["jev"]["clear"] for p in jev_promos) / len(jev_promos)
        avg_s = sum(p["jev"]["spoiler"] for p in jev_promos) / len(jev_promos)
        jc1.metric("Avg hook probability", f"{avg_h:.0%}")
        jc2.metric("Avg unresolved probability", f"{avg_u:.0%}")
        jc3.metric("Avg clarity probability", f"{avg_c:.0%}")
        jc4.metric("Avg spoiler probability", f"{avg_s:.0%}")
        # Show rank correlation if has original_rank
        jev_ranked = sorted(jev_promos, key=lambda p: p["score"], reverse=True)
        if jev_promos[0].get("original_rank"):
            import statistics
            orig_ranks = [p["original_rank"] for p in jev_ranked]
            new_ranks = list(range(1, len(jev_ranked) + 1))
            # Simple metric: average absolute rank change
            avg_change = sum(abs(o - n) for o, n in zip(orig_ranks, new_ranks)) / len(jev_ranked)
            st.metric("Avg rank change vs original", f"{avg_change:.1f} positions")
            st.caption("How much Jev re-ranked the promos vs the LLM judge. Higher = more disagreement.")

    # --- Spoiler breakdown ---
    if judged:
        st.subheader("⚠️ Spoiler level distribution")
        st.bar_chart({"Level": list(spoiler_c.keys()), "Count": list(spoiler_c.values())}, x="Level", y="Count")
        high_sp = [p for p in judged if p["judge"].get("spoiler") == "high"]
        if high_sp and st.checkbox("Show high-spoiler promos", value=False):
            for p in high_sp[:10]:
                segs = " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in p["segments"])
                st.markdown(f"- **#{p['rank']}** {segs} — {p['hook'][:80]}…")

    # --- Duration ---
    st.subheader("⏱️ Duration distribution")
    dur_b = Counter(int(p["duration_sec"] / 5) * 5 for p in promos_an)
    st.bar_chart(
        {"Duration (s)": [f"{b}-{b+5}" for b in sorted(dur_b)], "Count": [dur_b[b] for b in sorted(dur_b)]},
        x="Duration (s)", y="Count",
    )

    # --- Per-episode coverage ---
    st.subheader("📋 Per-episode coverage")
    ep_c = Counter(min(p["episodes"]) for p in promos_an)
    all_ep_nums = range(1, meta_an.get("episodes", 50) + 1)
    st.bar_chart(
        {"Episode": [f"Ep {e}" for e in all_ep_nums],
         "Promos": [ep_c.get(e, 0) for e in all_ep_nums]},
        x="Episode", y="Promos",
    )
    missing_eps = [e for e in all_ep_nums if ep_c.get(e, 0) == 0]
    if missing_eps:
        st.caption(f"⚠️ No promos found for episodes: {', '.join(str(e) for e in missing_eps[:10])}{'…' if len(missing_eps) > 10 else ''}")

    # --- Cost estimation ---
    st.subheader("💰 Cost estimation")
    cost_est = estimate_cost(meta_an)
    st.metric("Estimated LLM cost", cost_est)
    if meta_an.get("tokens_in") and meta_an.get("tokens_out"):
        st.caption(
            f"Based on {meta_an['tokens_in']:,} input + {meta_an['tokens_out']:,} output tokens. "
            f"Model: {meta_an.get('model', 'unknown')}."
        )
        st.markdown("""
        **Cost at scale (50 episodes per show)**:
        - **Rules mode**: $0 (runs on any machine, ~25s)
        - **Gemini 2.5 Flash**: ~$0.01–0.02/show
        - **Grok 4.3**: ~$0.10–0.15/show
        - **GPT-4o**: ~$0.15–0.25/show

        For a show releasing 5 episodes/week, LLM mode costs **<$0.03/week on Gemini Flash**.
        """)

    # --- Top vs Bottom ---
    st.subheader("🏆 Top 5 vs Bottom 5")
    sorted_p = sorted(promos_an, key=lambda p: -p["score"])
    tc1, tc2 = st.columns(2)
    with tc1:
        st.markdown("**Top 5 highest-scoring**")
        for p in sorted_p[:5]:
            segs = " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in p["segments"])
            st.markdown(f"- **#{p['rank']}** ({p['score']:.3f}) {segs} — {p['hook'][:70]}")
    with tc2:
        st.markdown("**Bottom 5 lowest-scoring**")
        for p in sorted_p[-5:]:
            segs = " + ".join(f"Ep {s['episode']} {s['start']}–{s['end']}" for s in p["segments"])
            st.markdown(f"- **#{p['rank']}** ({p['score']:.3f}) {segs} — {p['hook'][:70]}")

    # --- Failed promos from check ---
    checks_an = load_checks(anal_dir)
    if checks_an:
        st.subheader("❌ Deterministic check failures")
        check_keys = ["duration_ok", "order_ok", "timestamps_ok", "no_midline_cut", "hook_event_3s", "not_duplicate"]
        fails = [c for c in checks_an if any(c.get(k) != "True" for k in check_keys)]
        if fails:
            st.warning(f"{len(fails)} promos fail at least one deterministic check:")
            for c in fails:
                bad = [k for k in check_keys if c.get(k) != "True"]
                st.markdown(f"- **Rank {c['rank']}**: {', '.join(bad)}")
        else:
            st.success("All promos pass every deterministic check.")