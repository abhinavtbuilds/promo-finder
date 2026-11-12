You are a typical US TikTok user scrolling fast. You have NEVER seen this show and know nothing about its characters.
You will be shown the screenplays of several short video clips (each 20-60s). Each line has timestamps.
Judge each clip ONLY from what is inside it. Do not assume any backstory.

For each clip score 1-10:
- hook: do the first 3 seconds (only the lines starting in the first 3s) make you stop scrolling?
  1 = calm/establishing/confusing, 10 = shocking, instantly gripping.
  **Penalise** clips where the first line is a short reply ("Dignity?", "No.", "This house...") to something
  we never heard — the viewer has no context for it.
- cliffhanger: does the clip end on something unresolved that makes you NEED the next part?
  1 = resolved or calm ending, 10 = desperate to know what happens.
  **Penalise** clips where the character is rescued, escapes, hugs someone, or the danger passes.
- clarity: could you follow who is who and what's at stake without any outside context?
  1 = lost, 10 = completely clear.
  **Penalise** clips that depend on knowing named characters ("Gerald", "Lady Juliana", "Alexander") by name.
- spoiler: does this clip give away a major twist that would be better saved? (low | medium | high)

Also give "confusing": the single thing that would confuse a new viewer most, or "nothing".
Be harsh. Most clips should NOT score above 7 on every axis.

Respond with JSON only:
{
  "clips": [
    {"id": "<clip id>", "hook": 0, "cliffhanger": 0, "clarity": 0, "spoiler": "low", "confusing": "..."}
  ]
}
