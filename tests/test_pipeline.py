"""Offline tests: parser invariants + full LLM path with a fake model (no API key needed).

    python -m pytest -q tests      (or)      python tests/test_pipeline.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from promo_finder import llm, pipeline  # noqa: E402
from promo_finder.candidates import cut_points, episode_candidates  # noqa: E402
from promo_finder.parse import load_show  # noqa: E402

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


if __name__ == "__main__":
    import tempfile
    test_parser_invariants()
    test_candidates_are_legal()
    with tempfile.TemporaryDirectory() as d:
        test_llm_path_with_fake_model(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_plain_format_episode(Path(d))
    print("all tests passed")
