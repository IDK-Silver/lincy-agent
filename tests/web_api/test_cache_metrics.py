from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from chat_agent.session.schema import SessionMetadata
from chat_web_api.cache import MetricsCache, ResponseMetrics
from chat_web_api.session_reader import SessionFiles


def _meta(session_id: str) -> SessionMetadata:
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    return SessionMetadata(
        session_id=session_id,
        user_id="u1",
        display_name="User",
        created_at=now,
        updated_at=now,
        status="active",
    )


def test_session_summary_uses_read_cache_rate_and_marks_codex_write_unmeasurable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="codex",
            model="gpt-5.4",
            prompt_tokens=1000,
            completion_tokens=100,
            cache_read_tokens=900,
            cache_write_tokens=0,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]
    cache._turns["s1"] = []

    summary = cache.get_session_summary("s1")

    assert summary is not None
    assert summary.read_cache_rate == 0.9
    assert summary.write_cache_measurable is False


def test_all_requests_reports_openrouter_write_cache_as_measurable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="openrouter",
            model="claude-sonnet-4.5",
            prompt_tokens=2000,
            completion_tokens=100,
            cache_read_tokens=1000,
            cache_write_tokens=128,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))

    assert rows[0]["read_cache_rate"] == 0.5
    assert rows[0]["write_cache_measurable"] is True


def test_all_requests_reports_pricing_source_status():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_tokens=2000,
            completion_tokens=100,
            cache_read_tokens=1000,
            cache_write_tokens=0,
            latency_ms=100,
            cost=0.001,
            turn_id="turn_000001",
            pricing_source="local_override",
            pricing_source_url="https://api-docs.deepseek.com/quick_start/pricing",
            pricing_stale=False,
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))
    summary = cache.get_session_summary("s1")

    assert rows[0]["pricing_source"] == "local_override"
    assert rows[0]["pricing_stale"] is False
    assert summary is not None
    assert summary.pricing_sources == [
        {
            "source": "local_override",
            "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
            "stale": False,
            "count": 1,
        }
    ]


def test_ollama_session_summary_marks_read_cache_rate_unavailable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="ollama",
            model="kimi-k2.6:cloud",
            prompt_tokens=1000,
            completion_tokens=100,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]
    cache._turns["s1"] = []

    summary = cache.get_session_summary("s1")

    assert summary is not None
    assert summary.read_cache_rate is None


def test_all_requests_reports_ollama_read_cache_rate_unavailable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="ollama",
            model="kimi-k2.6:cloud",
            prompt_tokens=2000,
            completion_tokens=100,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))

    assert rows[0]["read_cache_rate"] is None
