"""Insights agent: stays inside LTM's real GCC Origin catalogue and never loses an item."""
from conftest import FAKE_INSIGHT, make_gcc

from gcc_agents import insights_agent, ltm


def test_clean_keeps_recommendations_inside_the_catalogue():
    drifted = FAKE_INSIGHT.model_copy(update={
        "services": ["Talent & workforce", "Quantum consulting", "Transfer"], "engagement_model": "Outsourcing deal",
        "products": ["A", " ", "B", "C", "D", "E", "F"], "talking_points": ["1", "2", "3", "4"],
    })
    clean = insights_agent._clean(drifted)
    assert clean.services == ["Talent & workforce", "Transfer"]
    assert clean.engagement_model in ltm.ENGAGEMENT_MODELS
    assert len(clean.products) == 5 and len(clean.talking_points) == 3


def test_enrich_fills_missing_keeps_existing_and_survives_errors(monkeypatch, settings):
    have = make_gcc(company="Has", insight=FAKE_INSIGHT.model_copy(update={"fit": "Low"}))
    need = make_gcc(company="Needs")
    broken = make_gcc(company="Broken")

    def analyse(s, item):
        if item.company == "Broken":
            raise RuntimeError("boom")
        return FAKE_INSIGHT

    monkeypatch.setattr(insights_agent, "analyse", analyse)
    out = {i.company: i for i in insights_agent.enrich(settings, [have, need, broken])}
    assert out["Has"].insight.fit == "Low"          # not re-analysed
    assert out["Needs"].insight == FAKE_INSIGHT
    assert out["Broken"].insight is None             # still reported, just without a brief


def test_catalogue_matches_the_launch_release():
    for name in ("Design & advisory", "Talent & workforce", "AI agents & studios", "Operate & transform", "Transfer"):
        assert name in ltm.SERVICES
    assert "Build-Operate-Transform-Transfer" in ltm.ENGAGEMENT_MODELS and "Virtual GCC" in ltm.ENGAGEMENT_MODELS
    assert "300+" in ltm.CATALOGUE_TEXT and "BlueVerse" in ltm.CATALOGUE_TEXT
