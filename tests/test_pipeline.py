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

DATA = ROOT / "data" / "episodes"


class FakeClient:
    """Mimics the model: proposes one good window, one slightly-off window, one impossible window."""

    def __init__(self, cfg):
        self.calls = self.tokens_in = self.tokens_out = 0

    def json(self, system, user):
        self.calls += 1
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


if __name__ == "__main__":
    import tempfile
    test_parser_invariants()
    test_candidates_are_legal()
    with tempfile.TemporaryDirectory() as d:
        test_llm_path_with_fake_model(Path(d))
    print("all tests passed")
