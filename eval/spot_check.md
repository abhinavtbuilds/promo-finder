# Manual spot-check: top 10 promos (Grok 4.3 run of 8 Nov, after dedupe + spoiler fix; ranks may shift slightly on reruns, so clips are identified by episode and time)

Each promo was read against the screenplay.
- **Hook:** does something gripping happen in the first 3 seconds?
- **End:** does it stop on an unresolved moment?
- **Stranger:** could someone who has never seen the show follow it?

| # | Promo | Hook ≤3s | Unresolved end | Makes sense to a stranger | Verdict |
|---|---|---|---|---|---|
| 1 | Ep 15 00:44.20–01:26.00 (42s) | ✅ "Get out of the way!" then she refuses | ✅ "You want to fight us, veteran?" | ✅ thugs vs. injured woman, rescuer arrives | **Pass** |
| 2 | Ep 32 01:59.50–02:34.38 (35s) | ✅ "Silence! Silence!" | ✅ red-hot iron stops at her eye | ✅ mostly (the "title" line needs context) | **Pass** |
| 3 | Ep 16 00:14.30–00:55.20 (41s) | ✅ club caught bare-handed | ⚠️ ends on a confession naming "Gerald Harrington" | ❌ the payoff is a name a stranger doesn't know | **Partial**: needs context |
| 4 | Ep 3 01:24.00–02:15.50 (52s) | ✅ the stranger knocks out a guard | ✅ kneels with a ring, crowd gasps | ✅ the mystery of who he is works as a hook | **Pass** |
| 5 | Ep 18 01:23.50–01:54.00 (31s) | ✅ she screams as the beam splits | ❌ they escape through the window, so it's resolved | ✅ | **Fail**: resolved ending |
| 6 | Ep 2 01:14.20–01:42.80 (29s) | ❌ opens on "Dignity?", a reply to a line the clip cuts off | ⚠️ stops mid-sentence ("without a husband…") | ⚠️ the setup line is missing | **Fail**: needs context, cut mid-sentence |
| 7 | Ep 36 02:51.00–03:20.75 (30s) | ✅ "They stole my home!" | ✅ vow of revenge | ❌ ends on "their dead Duke is coming home", the series' big twist | **Fail**: spoiler-heavy (the judge rated it only "medium") |
| 8 | Ep 22 01:32.00–02:11.00 (39s) | ❌ she looks up at a ceiling: "This house…" | ✅ "Who has been treating you?" | ⚠️ who the Duchess is | **Fail**: weak hook |
| 9 | Ep 9 00:03.50–00:54.00 (51s) | ⚠️ the frantic knocking happens just *before* the clip; the mother's cry comes at 6s | ✅ "Why isn't it working?" | ✅ | **Partial**: start 2s too late |
| 10 | Ep 41 00:57.80–01:36.09 (38s) | ✅ chained woman's defiant line | ✅ doors explode, armed men pour in | ✅ | **Pass** |

**Score: 4 of 10 pass on all three questions, 2 are partial, 4 fail.** By question: hook 7/10, unresolved end 7/10, makes sense to a stranger 6/10.

## Patterns behind the failures (and the fix each points to)

1. **The opening is chosen one line too late or too early** (#6, #9). The LLM picks a start line, and code snaps it to a shot boundary but never checks what came just before. Fix: a code check that warns when the first line is a reply ("Dignity?", "No.") or when a loud sound effect ends within 1s before the start.
2. **The spoiler judgement is too lenient on the main twist** (#7). The cold viewer doesn't know which reveals matter. Fix: give it a short list of the series' key reveals (one line per arc, written by an editor) to check against.
3. **The ending resolves the problem** (#5). The rescue succeeds on screen. Fix: in pass 2, trim the end back to the last unresolved beat (here, the beam crashing down).
4. **The payoff depends on a name** (#3). Fix: the cold viewer already flagged it ("who Gerald Harrington is"); low clarity should weigh more in the ranking.

The deterministic checker (`python -m promo_finder.check`) passed all 122 promos on length, order, real timestamps and no mid-line cuts. It also found **20 near-duplicates**, now removed in code. A mid-*sentence* cut that falls between two lines (#6) is something only a human or the LLM catches.
