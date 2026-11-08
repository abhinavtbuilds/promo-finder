"""Parse Kuku screenplay .txt files into timestamped events.

Pure code, no AI: the format is regular, so rules are cheaper and exact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

TS = r"(\d+):(\d+(?:\.\d+)?)"
BLOCK_RE = re.compile(r"^\*\*(?P<head>.+?):\*\*\s*$")
TIME_RE = re.compile(rf"Start:\s*\({TS}\)\s*\|\s*End:\s*\({TS}\)", re.I)
SCENE_RE = re.compile(r"^SCENE\s+(\d+)", re.I)
KV_RE = re.compile(r"^(scene heading|scene start|scene end|episode number|episode logline|continuity notes)\s*:\s*(.*)$", re.I)
NON_DIALOGUE = {"CAMERA", "ACTION", "SOUND FX", "SFX", "MUSIC", "TEXT", "SUPER", "TITLE"}


def to_sec(m: str, s: str) -> float:
    return int(m) * 60 + float(s)


def fmt(sec: float) -> str:
    m, s = divmod(max(sec, 0.0), 60)
    return f"{int(m):02d}:{s:05.2f}"


@dataclass
class Event:
    episode: int
    scene: int
    kind: str            # dialogue | action | camera | sfx | other
    start: float
    end: float
    text: str
    speaker: str = ""    # dialogue only
    tone: str = ""       # parenthetical emotion, e.g. "Furious, Defiant"
    voice: str = ""      # V.O. / O.S.

    def line(self) -> str:
        """Human/LLM readable one-liner."""
        t = f"[{fmt(self.start)}-{fmt(self.end)}]"
        if self.kind == "dialogue":
            v = f" {self.voice}" if self.voice else ""
            tone = f" ({self.tone})" if self.tone else ""
            return f"{t} {self.speaker}{v}{tone}: {self.text}"
        return f"{t} {self.kind.upper()}: {self.text}"


@dataclass
class Scene:
    number: int
    heading: str
    start: float
    end: float


@dataclass
class Episode:
    number: int
    logline: str
    continuity: str
    scenes: list[Scene] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max([s.end for s in self.scenes] + [e.end for e in self.events] + [0])

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_head(head: str) -> tuple[str, str, str, str]:
    """'JULIANA (V.O.) (Reflective, Vulnerable)' -> (kind, speaker, voice, tone)."""
    parens = re.findall(r"\(([^)]*)\)", head)
    name = re.sub(r"\(.*", "", head).strip()
    upper = name.upper()
    if upper in NON_DIALOGUE:
        kind = {"CAMERA": "camera", "ACTION": "action", "SOUND FX": "sfx", "SFX": "sfx"}.get(upper, "other")
        return kind, "", "", ""
    voice, tone = "", []
    for p in parens:
        if p.strip().upper().rstrip(".") in {"V.O", "O.S", "O.C", "CONT'D"}:
            voice = p.strip()
        else:
            tone.append(p.strip())
    return "dialogue", name, voice, ", ".join(tone)


def parse_episode(path: str | Path) -> Episode:
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    ep = Episode(number=0, logline="", continuity="")
    scene: Scene | None = None
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1
        if not raw:
            continue
        kv = KV_RE.match(raw)
        if kv:
            key, val = kv.group(1).lower(), kv.group(2).strip()
            if key == "episode number":
                ep.number = int(re.sub(r"\D", "", val) or 0)
            elif key == "episode logline":
                ep.logline = val
            elif key == "continuity notes":
                ep.continuity = val
            elif scene and key == "scene heading":
                scene.heading = val
            elif scene and key in ("scene start", "scene end"):
                m = re.search(TS, val)
                if m:
                    setattr(scene, key.split()[1], to_sec(*m.groups()))
            continue
        sm = SCENE_RE.match(raw)
        if sm:
            scene = Scene(number=int(sm.group(1)), heading="", start=0.0, end=0.0)
            ep.scenes.append(scene)
            continue
        bm = BLOCK_RE.match(raw)
        if bm:
            kind, speaker, voice, tone = _parse_head(bm.group("head"))
            # next non-empty line: timestamps; then text until blank line
            while i < len(lines) and not lines[i].strip():
                i += 1
            tm = TIME_RE.search(lines[i]) if i < len(lines) else None
            if not tm:
                continue  # malformed block: skip rather than guess
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip() and not BLOCK_RE.match(lines[i].strip()):
                text_lines.append(lines[i].strip())
                i += 1
            text = " ".join(text_lines).strip().strip('"').strip("“”")
            ep.events.append(Event(
                episode=ep.number, scene=scene.number if scene else 0, kind=kind,
                start=to_sec(*tm.groups()[:2]), end=to_sec(*tm.groups()[2:]),
                text=text, speaker=speaker, tone=tone, voice=voice,
            ))
    if ep.number == 0:  # fall back to filename
        m = re.search(r"(\d+)", Path(path).stem)
        ep.number = int(m.group(1)) if m else 0
        for e in ep.events:
            e.episode = ep.number
    ep.events.sort(key=lambda e: (e.start, e.end))
    return ep


def load_show(folder: str | Path) -> list[Episode]:
    eps = [parse_episode(p) for p in sorted(Path(folder).glob("*.txt"))]
    return sorted(eps, key=lambda e: e.number)


if __name__ == "__main__":
    import sys
    for ep in load_show(sys.argv[1] if len(sys.argv) > 1 else "data/episodes"):
        kinds = {}
        for e in ep.events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        print(f"Ep {ep.number:>2}: {len(ep.scenes)} scenes, {len(ep.events)} events, "
              f"{fmt(ep.duration)} long, {kinds}")
