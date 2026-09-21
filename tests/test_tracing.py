"""LangSmith helpers: secrets never reach a span, and worker threads stay attached to their parent trace."""
import os
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from langsmith import traceable, tracing_context

from gcc_agents import tracing
from gcc_agents.config import Settings


class RecordingClient:
    """Minimal stand-in for langsmith.Client that records every span it is asked to create."""

    def __init__(self):
        self.runs = []
        self.tracing_queue = None
        self.info = mock.MagicMock()

    def create_run(self, **kwargs):
        self.runs.append(kwargs)

    def update_run(self, *a, **k):
        pass

    def __getattr__(self, name):  # any other client method is a no-op
        return mock.MagicMock()


def test_scrub_inputs_drops_secret_holders():
    s = Settings()
    s.openai_model = "m"
    out = tracing.scrub_inputs({"settings": s, "browser": object(), "query": "gcc india", "conn": 1})
    assert out == {"query": "gcc india"}


def test_init_tracing_states(monkeypatch):
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2",
                "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY", "LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    assert tracing.init_tracing().startswith("off")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert "LANGSMITH_API_KEY is missing" in tracing.init_tracing()
    assert os.environ["LANGSMITH_TRACING"] == "false"  # switched off so runs don't spam auth errors

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    assert tracing.init_tracing() == f"enabled: project={tracing.DEFAULT_PROJECT}"


def test_spans_nest_across_threads_and_exclude_settings():
    client = RecordingClient()
    s = Settings()
    s.openai_model = "SECRET-MARKER"

    @tracing.trace_tool("child", client=client)
    def child(settings, n):
        return n * 2

    @traceable(name="parent", client=client)
    def parent():
        with ThreadPoolExecutor(max_workers=3) as pool:
            return tracing.map_in_context(pool, lambda n: child(s, n), [1, 2, 3])

    with tracing_context(enabled=True, client=client):
        assert parent() == [2, 4, 6]

    by_name = {}
    for r in client.runs:
        by_name.setdefault(r["name"], []).append(r)
    assert len(by_name["parent"]) == 1 and len(by_name["child"]) == 3
    parent_id = by_name["parent"][0]["id"]
    assert all(c["parent_run_id"] == parent_id for c in by_name["child"])          # nested, not orphaned
    assert all("settings" not in c["inputs"] for c in by_name["child"])            # secrets stripped
    assert "SECRET-MARKER" not in str(client.runs)
