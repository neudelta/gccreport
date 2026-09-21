"""LangSmith tracing helpers.

LangChain and LangGraph calls (LLMs, graph nodes) are traced automatically once these env vars are set:
    LANGSMITH_TRACING=true   LANGSMITH_API_KEY=...   LANGSMITH_PROJECT=gcc-india-weekly
Everything else (Serper, Groq, Playwright scraping, URL checks, SMTP) is not a LangChain call, so we mark it
with `trace_tool` so it also shows up as a child span. Spans NEVER include API keys: any argument named
`settings`, `browser`, `context`, `conn` or `page` is stripped before it is sent.
"""
import contextvars
import logging
import os
from concurrent.futures import Executor
from typing import Any, Callable, Iterable

from langsmith import traceable

log = logging.getLogger("gcc")

DEFAULT_PROJECT = "gcc-india-weekly"
_UNSAFE_ARGS = {"settings", "browser", "context", "conn", "page", "self"}


def scrub_inputs(inputs: dict) -> dict:
    """Drop objects that hold secrets or live handles from a span's recorded inputs."""
    return {k: v for k, v in inputs.items() if k not in _UNSAFE_ARGS}


def trace_tool(name: str, run_type: str = "tool", **kwargs: Any) -> Callable:
    """`@langsmith.traceable` with secret-scrubbed inputs."""
    kwargs.setdefault("process_inputs", scrub_inputs)
    return traceable(name=name, run_type=run_type, **kwargs)


def map_in_context(pool: Executor, fn: Callable, items: Iterable) -> list:
    """Like pool.map, but every task runs in a copy of the caller's contextvars.

    LangSmith tracks the current parent span in a contextvar; plain thread-pool workers do not inherit it, so
    without this the spans created in worker threads would appear as separate, unrelated traces.
    """
    futures = [pool.submit(contextvars.copy_context().run, fn, item) for item in list(items)]
    return [f.result() for f in futures]


def _enabled() -> bool:
    return any(os.getenv(v, "").strip().lower() in ("1", "true", "yes", "on")
               for v in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2"))


def init_tracing() -> str:
    """Call once at startup (after .env is loaded). Returns a one-line status for the log."""
    if not _enabled():
        return "off (set LANGSMITH_TRACING=true and LANGSMITH_API_KEY to enable)"
    if not (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")):
        for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2"):
            os.environ[var] = "false"
        return "disabled: LANGSMITH_TRACING is on but LANGSMITH_API_KEY is missing"
    project = os.environ.setdefault("LANGSMITH_PROJECT", os.getenv("LANGCHAIN_PROJECT", DEFAULT_PROJECT))
    return f"enabled: project={project}"
