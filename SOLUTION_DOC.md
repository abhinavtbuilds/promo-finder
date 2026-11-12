# Promo Finder — Solution Document
**Abhinav Tanwar** · Kuku AI Generalist Assignment · Problem 1: Find the Promos · Nov 2026

---

## Problem choice and framing

**Why Problem 1.** It's the most concrete of the three problems — a real, daily task at Kuku
that an editor faces for every show. Solving it with a mix of code and AI felt like the best
demonstration of the skills the role requires: breaking down an editorial judgment into
verifiable pieces, building something practical, and knowing when to use AI vs deterministic
rules.

**Scope.** The tool reads a folder of timestamped screenplay files and produces a ranked list
of 20–60 second promo clips, each with hook, cliffhanger and a one-line rationale.

**Key assumptions (where the brief was silent):**
- **Promo = hook + cliffhanger + standalone.** A clip that opens with a stop-scrolling moment
  (≤3s), ends unresolved, and makes sense to a first-time viewer. This is the core editorial
  heuristic; different editors may weigh these differently.
- **20–60 second hard boundary.** Every platform (Instagram Reels, TikTok, YouTube Shorts)
  works in this range. Clips shorter than 20s don't give enough context; longer than 60s lose
  retention.
- **Camera-shot boundaries as cut points.** The tool snaps all times to real shot/event
  boundaries so an editor can cut exactly there. Cutting mid-line is never allowed.
- **Cross-episode promos are valid.** Joining a moment from episode N with a later moment
  from episode N/M is allowed and scored, with a penalty for the jump cut.
- **Spoilers from late episodes should be held back.** The first posting batch (typically 10
  promos) excludes clips from the back half of the show that reveal major twists.

**Tools used.** This README was written with AI assistance (Claude). The code was written
with AI assistance (Claude + in-house frameworks where noted). Prompts for the LLM
stages were iterated by hand.

---

## Approach and scale

### What's code (deterministic, zero API cost)

| Stage | What it does | Lines |
|---|---|---|
| Screenplay parser (`parse.py`) | Converts Kuku's block format and plain `[mm:ss]` format into timestamped events | 215 |
| Candidate generator (`candidates.py`) | All legal 20–60s windows snapped to shot boundaries, with transparent rule-based scoring | 257 |
| Non-max suppression | Drops near-duplicate windows (>40% overlap) | 15 |
| Validation (`snap_window`) | Snaps LLM proposals to real cut points, enforces 20–60s, drops invalid | 30 |
| Ranking + posting order | Blended score → greedy one-per-episode with spoiler holdback | 50 |
| Deterministic checks (`check.py`) | 6 pass/fail + 4 warnings per promo (duration, order, timestamps, mid-line cuts, hook-in-3s, duplicates) | 126 |
| Gold evaluation (`evaluate.py`) | Precision/recall against hand-labelled promos | 80 |

### What's AI (replaceable, model-agnostic)

| Stage | Prompt | What the LLM does | Cost driver |
|---|---|---|---|
| Editor | `prompts/editor.md` | Reads a screenplay + rule hints, proposes ≤3 promos with hook/cliffhanger/why | Input tokens (the screenplay) |
| Stitcher | `prompts/stitcher.md` | Proposes two-part cross-episode promos from single-episode picks | Small prompt + clip metadata |
| Cold viewer | `prompts/cold_viewer.md` | Scores each clip as a first-time viewer (hook, cliffhanger, clarity, spoiler) | Output tokens (per-clip scoring) |

**Model-agnostic design.** Swapping the model = editing `config.yaml`. Ready-made configs
for Groq (free), Gemini (free), OpenRouter, and local Ollama are in `configs/`. The code
never changes.

**Jev judge experiment.** A separate integration (`jev_rejudge.py`) replaces the LLM
cold-viewer with TypeSafe's Jev decision model, which answers yes/no questions with
probabilities instead of 1-10 scores. This is closer to how an editor actually decides.

### Where the human stays in the loop

1. The **Review tab** lets an editor approve or reject each promo. Verdicts are exported as
   CSV and become evaluation labels.
2. The **posting plan** separates promos into "post first" and "hold back" lists.
3. The **Quality tab** surfaces deterministic check failures and failure patterns so the
   editor knows what to watch for.

### Cost at real-world volume (50 episodes per show)

| Mode | Cost | Time | Notes |
|---|---|---|---|
| Rules only | **$0** | ~25s | No API calls. Runs on any machine. |
| Gemini 2.5 Flash | ~$0.01–0.02 | ~3 min | Free tier sufficient for the assignment |
| Grok 4.3 | ~$0.10–0.15 | ~1 min | Used for the main LLM run |
| Jev judge (replacing cold viewer) | ~$0.05 | ~1 min | Per-clip judge swap |

At scale (e.g. 500 episodes/week across 10 shows): rules mode is free; LLM mode on Gemini
Flash would cost ~$0.02/show → $0.20/week for all shows.

---

## Quality checking (three layers)

### 1. Deterministic checks (`check.py`)

Each promo is checked against 6 pass/fail criteria. **LLM run: 97/100 pass all 6.**

- `duration_ok`: 100/100
- `order_ok`: 100/100
- `timestamps_ok`: 100/100
- `no_midline_cut`: 100/100
- `hook_event_3s`: 97/100
- `not_duplicate`: 100/100

### 2. Gold evaluation (`evaluate.py`)

6 hand-labelled promos were verified against the screenplay. **LLM run: 67% recall@20**
(4 of 6 gold promos found in the top 20).

After **Jev re-judging**: **precision@5 rose from 0.4 → 0.6**, recall@5 from 0.33 → 0.5.
Jev correctly demoted promos with reply openings and resolved endings, and promoted
strong clips that were buried.

### 3. Human spot check

Of the top 10 promos, only 4 pass all three promo quality criteria when read against the
screenplay. This is the honest number — and it identifies the 4 failure patterns that the
next iteration should fix.

---

## Honest failures

A manual spot-check of the top 10 promos revealed 4 failure patterns:

1. **Opens one line too late.** The first dialogue is a short reply ("Dignity?", "This house...")
   to a line cut off. *Fix: already flagged by `warn_opens_on_reply`; Jev also catches it.*
2. **Spoiler judgment too lenient.** The cold viewer doesn't know which reveals are the
   show's main twists. *Fix: give it a short list of key reveals to check against.*
3. **Ending resolves the problem.** The rescue/escape completes on screen. *Fix: trim
   back to the last unresolved beat — Jev catches this (unresolved=11%).*
4. **Depends on named characters.** The cliffhanger references "Gerald Harrington" — a
   stranger has no context. *Fix: Jev correctly gives this a low clarity score.*

The deterministic checker catches structural issues well. The LLM/Jev judge catches
editorial issues sometimes. A human editor is still essential — but the tool reduces 50
episodes to ~15 promos that need their attention.

---

## The prototype

The Streamlit app (`app.py`) provides 6 tabs:

- **About** — problem framing, assumptions, AI/code split, scalability
- **Run** — upload episodes or use sample, pick rules/LLM/Jev mode, run, download results
- **Review** — browse, filter (score, verdict, episode, source, sort), approve/reject,
  deterministic check badges, screenplay preview
- **Compare** — side-by-side run comparison (metrics, overlap, score distribution)
- **Quality** — deterministic check results, gold evaluation, honest failure patterns
- **Analytics** — score distribution, judge scores, source mix, spoiler levels, cost estimates

**Deployment:** Streamlit Community Cloud or Hugging Face Spaces (free tier).
**Raw outputs:** `outputs/` contains 3 runs (rules, LLM, LLM+Jev) on the full 50-episode
input pack.

---

*Built with code + AI assistance. The code is at github.com/abhinavtbuilds/promo-finder.*