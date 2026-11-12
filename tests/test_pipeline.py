"""Offline tests: parser invariants + full LLM path with a fake model (no API key needed).

    python -m pytest -q tests      (or)      python tests/test_pipeline.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from promo_finder import llm, pipeline  # noqa: E402
from promo_finder.candidates import (  # noqa: E402
    SCORE_FLOOR, best_unresolved_end, cut_points, episode_candidates,
    _caption_text, _cliff_caption, nudge_back_past_reply,
    score_window, REPLY_LINES,
)
from promo_finder.parse import Episode, Event, fmt, load_show  # noqa: E402

DATA = ROOT / "data" / "sample"  # invented show shipped with the repo, so tests run on a clean clone


class FakeClient:
    """Mimics the model: proposes one good window, one slightly-off window, one impossible window."""

    def __init__(self, cfg):
        self.calls = self.tokens_in = self.tokens_out = 0

    def json(self, system, user):
        self.calls += 1
        if "TWO-PART" in system:
            return {"stitches": [{"a": 0, "a_end": "00:25.00", "b": 2, "b_start": "00:38.00",
                                  "hook": "h", "cliffhanger": "c", "why": "w"}]}
        if "promo editor" in system:
            return {"promos": [
                {"start": "00:10.00", "end": "00:45.00", "hook": "h", "cliffhanger": "c", "why": "w", "standalone_risk": "none"},
                {"start": "00:31.37", "end": "01:02.11", "hook": "h2", "cliffhanger": "c2", "why": "w2", "standalone_risk": "x"},
                {"start": "00:05.00", "end": "00:09.00", "hook": "too short", "cliffhanger": "", "why": "", "standalone_risk": ""},
            ]}
        n = user.count("=== CLIP id=")
        return {"clips": [{"id": i, "hook": 7, "cliffhanger": 6, "clarity": 8, "spoiler": "low", "confusing": "nothing"}
                          for i in range(n)]}


def test_parser_invariants():
    eps = load_show(DATA)
    assert len(eps) >= 1
    for ep in eps:
        assert ep.events, ep.number
        for e in ep.events:
            assert 0 <= e.start <= e.end <= ep.duration + 0.01, (ep.number, e)


def test_candidates_are_legal():
    for ep in load_show(DATA)[:10]:
        starts, ends = cut_points(ep)
        for c in episode_candidates(ep):
            s = c.segments[0]
            assert 20 <= s.duration <= 60
            assert s.start in starts and s.end in ends


def test_llm_path_with_fake_model(tmp_path):
    sub = tmp_path / "eps"
    sub.mkdir()
    for p in sorted(DATA.glob("*.txt"))[:3]:
        (sub / p.name).write_text(p.read_text())
    orig = llm.LLMClient
    llm.LLMClient = FakeClient
    try:
        out = pipeline.run(str(sub), str(tmp_path / "out"), "llm", str(ROOT / "config.yaml"))
    finally:
        llm.LLMClient = orig
    data = json.loads((out / "promos.json").read_text())
    promos = data["promos"]
    assert promos and all(20 <= p["duration_sec"] <= 60 for p in promos)
    assert all(p["judge"] for p in promos), "every clip should be judged"
    assert not any(p["hook"] == "too short" for p in promos), "invalid window must be dropped"
    assert any("snapped" in " ".join(p["notes"]) for p in promos), "off-grid times should be snapped"
    assert any(p["source"] == "llm-stitch" for p in promos), "LLM stitch should produce a validated 2-part promo"
    for p in promos:  # every promo, stitched or not, is in story order
        segs = [(s["episode"], s["start_sec"], s["end_sec"]) for s in p["segments"]]
        assert all((a[0], a[2]) <= (b[0], b[1]) for a, b in zip(segs, segs[1:]))


def test_plain_format_episode(tmp_path):
    """A different screenplay format (one timestamped line each, no camera blocks) still yields valid promos."""
    lines = ["EP 7", "[00:00] INT. HALL - NIGHT"]
    t = 0
    for i in range(40):
        t += 2
        lines.append(f"[00:{t:02d}] {'ANNA' if i % 2 else 'MARK'}: Line number {i}{'!' if i % 5 == 0 else '.'}")
    (tmp_path / "ep7.txt").write_text("\n".join(lines))
    out = pipeline.run(str(tmp_path), str(tmp_path / "out"), "rules", str(ROOT / "config.yaml"))
    promos = json.loads((out / "promos.json").read_text())["promos"]
    assert promos and all(20 <= p["duration_sec"] <= 60 for p in promos)


# ------------------------------------------------------------------ quality function tests


def test_score_floor_drops_weak_episode(tmp_path):
    """An episode with no strong windows returns 0 promos."""
    (tmp_path / "ep99.txt").write_text(
        "EPISODE NUMBER: Episode 99\n"
        "EPISODE LOGLINE: A quiet day.\n"
        "CONTINUITY NOTES: Test only.\n"
        "--- SCRIPT START ---\n"
        "SCENE 1\n"
        "Scene heading: A peaceful room\n"
        "Scene start: 00:00.00\n"
        "Scene end: 00:30.00\n"
        "**CAMERA:**\n"
        "Start: (00:00.00) | End: (00:10.00)\n"
        "Slow pan across a fireplace.\n"
        "**CHARACTER (Calm):**\n"
        "Start: (00:03.00) | End: (00:05.00)\n"
        '"It is a pleasant morning."\n'
        "**CHARACTER (Content):**\n"
        "Start: (00:08.00) | End: (00:12.00)\n"
        '"Indeed. The tea is lovely."\n'
        "**CAMERA:**\n"
        "Start: (00:10.00) | End: (00:30.00)\n"
        "Fade out peacefully.\n"
    )
    eps = load_show(str(tmp_path))
    assert len(eps) == 1
    cands = episode_candidates(eps[0])
    # All candidates should be below the floor and not returned
    assert len(cands) == 0, f"expected 0 promos for a calm episode, got {len(cands)}"


def test_nudge_back_past_reply():
    """A promo opening on a short reply should be nudged backward to include setup."""
    evs = [
        Event(1, 1, "camera", 5.0, 7.0, "Shot of the door opening."),
        Event(1, 1, "action", 6.0, 9.0, "Ethan enters."),
        Event(1, 1, "dialogue", 7.0, 8.5, "Dignity?", speaker="LANA"),
        Event(1, 1, "dialogue", 10.0, 13.0, "You don't deserve an answer.", speaker="ETHAN"),
    ]
    ep = Episode(number=1, logline="", continuity="")
    ep.events = evs
    ep.scenes = []

    reply_start = 7.0  # starts on "Dignity?"
    nudged = nudge_back_past_reply(ep, reply_start, 27.0)
    # "Dignity?" is a reply; with an earlier event at 6.0s, it should nudge back
    assert nudged is not None, "should nudge back to include the setup before the reply"
    assert nudged < reply_start, "nudged start should be earlier than the reply"


def test_caption_filters_camera_direction():
    """_caption_text should prefer dialogue over camera/action lines."""
    evs = [
        Event(1, 1, "camera", 0.0, 3.0, "Slow push in on a window."),
        Event(1, 1, "dialogue", 1.0, 4.0, "Where is my sister?", speaker="MIRA"),
        Event(1, 1, "action", 4.0, 6.0, "She slams the door."),
    ]
    cap = _caption_text(evs)
    assert "MIRA" in cap, "dialogue should be preferred over camera action"
    assert "MIRA: Where is my sister?" in cap


def test_cliff_caption_filters_cut_to_black():
    """_cliff_caption should skip 'cut to black' and prefer the last dialogue."""
    evs = [
        Event(1, 1, "dialogue", 10.0, 12.0, "You will never find him.", speaker="DORIAN"),
        Event(1, 1, "camera", 12.0, 14.0, "Cut to black."),
    ]
    cap = _cliff_caption(evs)
    assert "DORIAN" in cap
    assert "cut to black" not in cap.lower()


def test_reply_line_pattern():
    """REPLY_LINES matches short replies."""
    assert REPLY_LINES.match("Yes")
    assert REPLY_LINES.match("Dignity?")
    assert REPLY_LINES.match("No.")
    assert REPLY_LINES.match("Careful")
    assert not REPLY_LINES.match("I will never forgive you")


if __name__ == "__main__":
    import tempfile
    test_parser_invariants()
    test_candidates_are_legal()
    with tempfile.TemporaryDirectory() as d:
        test_llm_path_with_fake_model(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_plain_format_episode(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_score_floor_drops_weak_episode(Path(d))
    test_nudge_back_past_reply()
    test_caption_filters_camera_direction()
    test_cliff_caption_filters_cut_to_black()
    test_reply_line_pattern()
    print("all tests passed")
