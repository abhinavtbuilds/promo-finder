"""Generate promo candidate windows and score them with transparent rules.

Rules here are deliberately simple and explainable. They do two jobs:
 1. shortlist candidates cheaply so the LLM only reads a few per episode;
 2. act as a no-API baseline (and a sanity check on what the LLM picks).
"""
# Author: Abhinav Tanwar — Promo Finder, Kuku AI Generalist assignment (Nov 2026)
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parse import Episode, Event, fmt

MIN_LEN, MAX_LEN = 20.0, 60.0
HOOK_SECS = 3.0
TAIL_SECS = 5.0

# --- lexicons (tunable in one place) -------------------------------------------------
PHYSICAL = r"slap|punch|strike|struck|shove|kick|tackl|seize|grab|drag|choke|throat|lunge|charge|smash|slam|hurl|collapse|crash|blood|bleed|wound|fire|flame|blaz|inferno|burn|handcuff|arrest|gallows|hang|thorn|scream|gasp|weep|sob|faint|knife|gun|truncheon|club|pipe|explosion|shatter|freeze|kneel|proposal|ring"
HOT_TONE = r"furious|screaming|shouting|menacing|threatening|cruel|mocking|gloating|enraged|terrified|desperate|hysterical|venomous|vindictive|sadistic|brutal|panic|roaring|snarl|lethal|dangerous|heartbroken|sobbing|accusing|accusatory|agoniz|horror|shock|sinister|predatory"
CALM_TONE = r"gentle|tender|soft|warm|peaceful|content|reassuring|amused|playful|teasing|fond|grateful|relieved|relief|smil|calm|quiet|reflective|nostalgic|comfort"
CLIFF_CUES = r"cliffhanger|freeze-frame|freeze|cut to black|cuts to black|abrupt|suspense|dread|hangs|ominous|menacing hold|knife's edge"
RESOLVED_CUES = r"fade|fades|fading|smile|relief|peaceful|embrace|safe|grateful|content|laugh|escaped|rescued|survive|survived|safe now|all right|okay|it's over"
# Verbs/actions that signal a resolved ending (conflict resolved on screen)
RESOLVED_ACTIONS = r"embrace|hug|walk away together|ride off|escape through|climb out|pull.*safe|help.*up|reunite|kiss|hold hands|smile at each other"
BACKREF = r"\b(again|last time|still|after what|as before|remember|told you|the ring|the ledger|the watch)\b"
# genre tropes that sell microdramas regardless of the show (betrayal, rejection, secret identity, rescue)
TROPES = r"abandon|humiliat|disown|betray|plain|unworthy|beneath|divorce|marry|wedding|bride|proposal|accept my hand|one knee|pregnan|poison|witch|execut|chains|dungeon|duke|duchess|heir|the real|you're the|who are you|disguise|framed|liar|traitor|slave|maid|beggar|penniless"
ESTABLISHING = r"establishing shot|wide shot of the (?:manor|cottage|estate)|exterior shot|tilting up toward"
# Expanded list of reply-like utterances: 1-3 word replies that make no sense cut off from context
REPLY_LINES = r"^(yes|no|okay|sure|fine|right|indeed|never|maybe|perhaps|i am|it is|it's me|that's him|she is|he is|so was i|so were you|not yet|not now|of course|of course not|absolutely|never mind|my lady|your grace|sir|milady|madam|this house|that man|that woman|dignity|loyalty|honor|quiet|silence|no\.{3}|yes\.{3})$"


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.I))


def intensity(e: Event) -> float:
    """0..~3 rough 'stop-scrolling' energy of a single event."""
    t = f"{e.text} {e.tone}"
    s = 0.6 * min(_count(PHYSICAL, t), 3) + 0.5 * min(_count(HOT_TONE, e.tone), 2)
    if e.kind == "dialogue":
        if "!" in e.text:
            s += 0.4
        if "?" in e.text:
            s += 0.2
    if e.kind == "sfx" and re.search(r"crash|slam|scream|explos|shatter|punch|thud|bang", e.text, re.I):
        s += 0.5
    s += 0.5 * min(_count(TROPES, e.text), 2)
    s -= 0.4 * min(_count(CALM_TONE, e.tone), 2)
    return max(s, 0.0)


@dataclass
class Segment:
    episode: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def label(self) -> str:
        return f"Ep {self.episode} {fmt(self.start)}-{fmt(self.end)}"


@dataclass
class Candidate:
    segments: list[Segment]
    hook_score: float = 0.0
    cliff_score: float = 0.0
    standalone_score: float = 0.0
    energy_score: float = 0.0
    rule_score: float = 0.0
    hook_text: str = ""
    cliff_text: str = ""
    notes: list[str] = field(default_factory=list)
    source: str = "rules"

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def key(self) -> str:
        return " + ".join(s.label() for s in self.segments)


# --- cut points ----------------------------------------------------------------------
def cut_points(ep: Episode) -> tuple[list[float], list[float]]:
    """Legal start/end times: shot boundaries that don't slice a spoken line."""
    shots = [e for e in ep.events if e.kind == "camera"]
    if not shots:  # formats without camera blocks: any line/action boundary is a cut point
        shots = [e for e in ep.events if e.kind in ("dialogue", "action", "sfx")]
    dialog = [e for e in ep.events if e.kind == "dialogue"]
    starts = sorted({e.start for e in shots} | {0.0})
    ends = sorted({e.end for e in shots} | {ep.duration})

    def inside_line(t: float) -> bool:
        return any(d.start + 0.15 < t < d.end - 0.15 for d in dialog)

    return [t for t in starts if not inside_line(t)], [t for t in ends if not inside_line(t)]


def events_in(ep: Episode, a: float, b: float) -> list[Event]:
    return [e for e in ep.events if e.end > a and e.start < b]


def script_text(ep: Episode, a: float, b: float) -> str:
    return "\n".join(e.line() for e in events_in(ep, a, b))


# --- scoring -------------------------------------------------------------------------
def score_hook(ep: Episode, a: float) -> tuple[float, str]:
    # only events that *begin* inside the first 3s (a line already mid-way doesn't hook)
    evs = [e for e in events_in(ep, a, a + HOOK_SECS) if a - 0.3 <= e.start < a + HOOK_SECS]
    if not evs:
        return 0.0, ""
    s = max(intensity(e) for e in evs) + 0.3 * sum(intensity(e) for e in evs) / len(evs)
    first_line = next((e for e in evs if e.kind == "dialogue"), None)
    if first_line:
        s += 0.3  # someone speaks in the first 3s: viewers stop for faces + words
        if re.match(REPLY_LINES, first_line.text.strip(), re.I):
            s -= 1.0  # reply to a question we never heard — viewer is lost
        # Check if the line trails in (ellipsis start) — it's mid-conversation
        if first_line.text.strip().startswith(("...", "…")):
            s -= 0.6
    # Check if the best action happens just BEFORE the clip (0-2s before start)
    # This means the hook momentum starts at the wrong moment
    pre_evs = [e for e in events_in(ep, max(0, a - 2.0), a) if e.kind in ("action", "sfx")]
    if pre_evs and max(intensity(e) for e in pre_evs) > max((intensity(e) for e in evs), default=0):
        s -= 0.6  # the real hook action was just before the clip — we started too late
    if any(re.search(ESTABLISHING, e.text, re.I) for e in evs):
        s -= 0.8
    best = max(evs, key=intensity)
    return s, (first_line or best).line()


def score_cliff(ep: Episode, b: float) -> tuple[float, str]:
    evs = [e for e in events_in(ep, b - TAIL_SECS, b)]
    if not evs:
        return 0.0, ""
    text = " ".join(f"{e.text} {e.tone}" for e in evs)
    s = 0.5 * max(intensity(e) for e in evs)
    s += 0.6 * min(_count(CLIFF_CUES, text), 2)
    last_line = next((e for e in reversed(evs) if e.kind == "dialogue"), None)
    if last_line:
        if last_line.text.rstrip().endswith("?"):
            s += 0.8  # an unanswered question
        if last_line.text.rstrip().endswith(("...", "…")):
            s += 0.5
        if re.search(HOT_TONE, last_line.tone, re.I):
            s += 0.4
    s -= 0.4 * min(_count(RESOLVED_CUES, text), 3)
    # ** New: penalize action-based resolution **
    # Check the very last events (last 2s): if the main action is resolved (escape, rescue, embrace),
    # the clip has no cliffhanger
    last_evs = [e for e in evs if b - 2.0 <= e.end <= b and e.kind in ("action", "camera")]
    if last_evs:
        last_text = " ".join(e.text for e in last_evs)
        s -= 0.6 * min(_count(RESOLVED_ACTIONS, last_text), 2)
    if abs(b - ep.duration) < 0.5:
        s += 0.5  # writers design episode endings as cliffhangers
    anchor = last_line or evs[-1]
    return s, anchor.line()


def score_standalone(ep: Episode, a: float, b: float) -> tuple[float, list[str]]:
    evs = events_in(ep, a, b)
    dialog = [e for e in evs if e.kind == "dialogue"]
    notes, s = [], 1.0
    refs = sum(_count(BACKREF, e.text) for e in dialog)
    if refs:
        s -= 0.25 * min(refs, 3)
        notes.append(f"{refs} back-reference(s) to earlier plot")
    # ** New: penalize proper-noun character names that a stranger won't know **
    # Known show terms (episode keywords like "duke", "duchess", "lord", "lady") are fine;
    # specific character names like "Gerald", "Harrington", "Alexander", "Juliana" are not
    all_dialogue_text = " ".join(e.text for e in dialog)
    # Find capitalized words that look like character names (not at line start)
    proper_names = re.findall(r'(?<![A-Za-z])([A-Z][a-z]{3,})(?![A-Za-z])', all_dialogue_text)
    # Filter out title words and common words
    title_words = {"Lord", "Lady", "Duke", "Duchess", "Sir", "Madam", "Miss", "Master",
                   "Your", "His", "Her", "My", "Father", "Mother", "Brother", "Sister",
                   "Doctor", "Captain", "Colonel", "Sergeant", "Inspector", "King", "Queen",
                   "Prince", "Princess", "Count", "Countess", "God", "Heaven", "Please"}
    named_refs = [n for n in proper_names if n not in title_words]
    if named_refs:
        penalty = 0.15 * min(len(named_refs), 4)
        s -= penalty
        notes.append(f"references character names a stranger won't know: {', '.join(sorted(set(named_refs))[:3])}")
    voiceover = sum(1 for e in dialog if e.voice)
    if voiceover >= 2:
        s -= 0.3
        notes.append("relies on voice-over narration")
    if len(dialog) < 3:
        s -= 0.4
        notes.append("little dialogue: hard to follow without visuals")
    speakers = {e.speaker for e in dialog}
    if len(speakers) >= 2:
        s += 0.2  # visible conflict between two people reads without context
    return s, notes


def score_window(ep: Episode, a: float, b: float) -> Candidate:
    h, htxt = score_hook(ep, a)
    c, ctxt = score_cliff(ep, b)
    st, notes = score_standalone(ep, a, b)
    evs = events_in(ep, a, b)
    energy = sum(intensity(e) for e in evs) / max(b - a, 1) * 10  # per 10s
    length_pen = 0.01 * max(0, (b - a) - 45)  # mild preference for tighter clips
    total = 1.4 * h + 1.2 * c + 0.8 * st + 0.4 * energy - length_pen
    return Candidate(
        segments=[Segment(ep.number, a, b)], hook_score=round(h, 2), cliff_score=round(c, 2),
        standalone_score=round(st, 2), energy_score=round(energy, 2), rule_score=round(total, 3),
        hook_text=htxt, cliff_text=ctxt, notes=notes,
    )


def _overlap(x: Candidate, y: Candidate) -> float:
    tot = 0.0
    for s in x.segments:
        for t in y.segments:
            if s.episode == t.episode:
                tot += max(0.0, min(s.end, t.end) - max(s.start, t.start))
    return tot / min(x.duration, y.duration)


def nms(cands: list[Candidate], max_overlap: float = 0.4, keep: int = 5) -> list[Candidate]:
    """Non-max suppression: drop near-duplicate windows."""
    out: list[Candidate] = []
    for c in sorted(cands, key=lambda c: c.rule_score, reverse=True):
        if all(_overlap(c, o) <= max_overlap for o in out):
            out.append(c)
        if len(out) >= keep:
            break
    return out


def episode_candidates(ep: Episode, keep: int = 5) -> list[Candidate]:
    starts, ends = cut_points(ep)
    cands = [score_window(ep, a, b) for a in starts for b in ends if MIN_LEN <= b - a <= MAX_LEN]
    return nms(cands, keep=keep)


# --- simple cross-episode stitches ---------------------------------------------------
def stitched_candidates(eps: list[Episode], keep: int = 6) -> list[Candidate]:
    """Hook moment from episode N + ending of episode N or N+1 (setup -> payoff tease).

    Kept simple on purpose: a short (6-20s) high-energy opening paired with the last
    15-40s of the same or next episode, total 20-60s, only moving forward in time.
    """
    by_num = {e.number: e for e in eps}
    out: list[Candidate] = []
    for ep in eps:
        starts, ends = cut_points(ep)
        hooks = []
        for a in starts:
            for b in ends:
                if 6 <= b - a <= 20:
                    h, htxt = score_hook(ep, a)
                    hooks.append((h, a, b, htxt))
        hooks = sorted(hooks, reverse=True)[:3]
        for nxt in (ep.number, ep.number + 1):
            tgt = by_num.get(nxt)
            if not tgt:
                continue
            t_starts, _ = cut_points(tgt)
            for h, a, b, htxt in hooks:
                for ts in t_starts:
                    tail = tgt.duration - ts
                    if not 15 <= tail <= 40:
                        continue
                    if nxt == ep.number and ts < b + 10:
                        continue  # same episode: must skip ahead, else it's just one stretch
                    total = (b - a) + tail
                    if not MIN_LEN <= total <= MAX_LEN:
                        continue
                    c, ctxt = score_cliff(tgt, tgt.duration)
                    st1, n1 = score_standalone(ep, a, b)
                    st2, n2 = score_standalone(tgt, ts, tgt.duration)
                    st = (st1 + st2) / 2 - 0.2  # every jump cut costs some clarity
                    score = 1.4 * h + 1.2 * c + 0.8 * st
                    out.append(Candidate(
                        segments=[Segment(ep.number, a, b), Segment(tgt.number, ts, tgt.duration)],
                        hook_score=round(h, 2), cliff_score=round(c, 2), standalone_score=round(st, 2),
                        rule_score=round(score, 3), hook_text=htxt, cliff_text=ctxt,
                        notes=n1 + n2 + ["stitched: check the jump makes sense"], source="rules-stitch",
                    ))
    return nms(out, max_overlap=0.3, keep=keep)
