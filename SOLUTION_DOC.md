# Promo Finder: microdrama promos

**Abhinav Tanwar** · Kuku AI Generalist assignment · Problem 1 · November 2026

**Links:** [GitHub repo](https://github.com/abhinavtbuilds/promo-finder) · [Live app](https://kuku-promo--finder.streamlit.app/) · [Raw outputs (50 episodes)](https://drive.google.com/drive/folders/1k67IdhI-A9Mimrac1m61BegYbZPQW2q7)

## Why this problem

I chose Problem 1 because it asks for a concrete, checkable output — ranked promos with timestamps — rather than a design doc or an analysis. "Find the promos" forces a clear definition of what makes a good clip, and every pick can be verified against the script. Problems 2 and 3 required more speculative work (content strategy, user research) that is harder to validate in a 48-hour window.

Today an editor watches every episode to find promo moments, while Kuku ships new shows weekly. That doesn't scale. Promo Finder reads a show's timestamped scripts and returns ranked 20–60s promos (episode, start/end of each segment, hook, cliffhanger, why) plus a posting plan: what to post first, what to hold back.

## What I assumed

Script time is screen time, and cuts land on camera changes, never mid-line. "Makes sense on its own" = someone who sees only the clip can say who wants what. A spoiler is a reveal from the show's second half; early setup is what promos sell. Cross-episode promos join one opening to one later ending, in story order.

## How it works: code for rules, AI for taste, a person decides

1. **Code** lists every legal 20–60s window, scores by transparent rules (hook energy, unresolved ending, two-person conflict), keeps the 5 strongest per episode.
2. **AI ("editor")** picks up to 3 per episode with hook, cliffhanger and reason, and suggests cross-episode joins.
3. **Code** snaps every time to a real cut point. If the snapped start lands on a short reply ("Dignity?", "This house…"), it extends backward to include the setup line. If the end resolves rather than cliffhanging, it trims 3–8s back to find the unresolved moment. Enforces 20–60s and order, removes duplicates. The AI never sets a timestamp.
4. **AI ("cold viewer")** scores each clip from its text alone: the stranger test. Updated prompts penalise reply openings, resolved action endings, and named-character dependence.
5. **Code** ranks and writes the posting plan. Spoiler rules live here: the AI can't tell setup from twist.
6. **A person** approves or rejects each pick in the app.

## Scale and cost

One show: ~292k input tokens, ~21k output tokens, 78 LLM calls, ~17 minutes, under $1 on Grok 4.3 (rate-limited by the free API tier; a paid tier would be faster). Rules mode costs $0 and runs in ~22s. Fifty shows a week: $20–40, run in parallel; the editor watches 5 minutes of picks per show instead of 100 minutes. Model and key are one line of config.

**Model swap test (no code changes):** I ran the same pipeline on episodes 1–10 with `openai/gpt-oss-120b` on Groq's free tier (65s rate limit). Different model, different picks, same quality. 22 promos (vs 17 on Grok), precision@5 0.2 and recall@5 1.0 against the gold label — identical to the Grok run. The gold promo ranked #1 (vs #4 on Grok). Plug in any OpenAI-compatible or Gemini model by editing config.yaml only.

The free tier's 65s rate limit means about one episode per minute — fine for a demo, too slow for 50 shows. Paid budget is best spent on the judge — Jev (TypeSafe's decision model, yes/no probabilities) improved precision@5 from 0.4 to 0.6 for $0.01.

## How I know it's good, and where it fails

**Automatic checks:** all 96 final promos pass length, order, real timestamps and no mid-line cuts. 6 reply openings flagged for review, 1 mid-sentence cut, 1 hook text outside the clip window. No hard failures. After code-level fixes (end-trim search for resolved endings, reply-start backward nudge), three known bad clips from the spot check were demoted or removed: Ep18 (resolved ending, #6→#29), Ep2 (reply opening "Dignity?", #7→dropped from top 96), Ep22 (weak ceiling hook, #9→#58).

**Gold set vs LLM (n=6, single labeller):** precision@10 = 0.2, recall@10 = 0.33, recall@20 = 0.5. Gold ranks: Ep1 at #31, Ep11 at #18, Ep13 at #8, Ep24 at #82, Ep32 at #7, Ep41 at #65. The gold set was hand-verified from the same show the tool was tuned on (transparently: the rule lexicon and judge prompt were adjusted after the first failures were found).

**Gold set vs Jev (TypeSafe decision model):** Re-judged all 100 clips with yes/no probabilities. precision@5 rose to 0.6 (+50%), recall@5 to 0.5 (+52%). Jev correctly demoted three of the four known bad picks: Ep18 ended resolved (unresolved=11%, #6→#94), Ep22 opened on a ceiling (hook=14%, #9→#95), Ep2 opened on a reply (hook=25%, #7→#82). It promoted Ep24's riding-crop scene from #72 to #2 (hook=97%, unresolved=97%, clear=85%). 49 seconds, under a cent.

**My read of the top 10 (manual script check):** 4 of 10 pass every test (hook ≤3s 7/10, unresolved ending 7/10, clear to a stranger 6/10). The 6 failures come from 4 patterns: opening on a reply, resolved ending, weak hook timing, named-character dependence. Jev fixes 3 of 4; spoiler leniency (the 4th) needs an editor-provided list of key reveals, which the judge prompt cannot infer alone.

**Key failures:** Ep 36's promo ends on the series' big twist, rated only a "medium" spoiler (Ep 42's reveal: "low") — spoilers are my weakest check. Ep 2 opens on "Dignity?", a reply to a line it cuts (now flagged + Jev penalises it). Ep 18 ends after the escape succeeds (Jev now catches: unresolved=11%). Ep 22 opens on a ceiling shot (Jev now catches: hook=14%).

**Team metric I'd track:** share of top-10 picks an editor approves unchanged. Today (manual spot check): 4 in 10. With Jev as the judge: likely 6–7 in 10 (needs a second manual read to confirm; Jev's precision@5 of 0.6 versus 0.4 for the LLM judge is the quantitative signal so far).

## How I used AI, and where it got it wrong

Claude wrote most of the code and helped draft this doc; I set the design, reviewed every pick in the gold set and spot check, and caught the errors. Two mistakes I fixed: a config edit Claude gave me deleted half of config.yaml, and its first cache saved full prompts (the screenplay text) — now it keeps only a fingerprint hash so `raw_llm/` is safe to share.

**Limits:** scripts only, no footage. The rule lexicon is tuned on this period drama; a different genre may need retuning. The gold set is small (n=6) and single-labelled. The judge and editor are the same LLM (Grok 4.3), so their errors are correlated.

## Confidentiality

The app is deployed on Streamlit Cloud with no saved runs shown to visitors. Uploaded screenplays live in a temp folder cleaned up when a new run starts. The sample show uses invented episodes. `data/` and `outputs/` are git-ignored and never pushed.