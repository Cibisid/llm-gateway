"""Tests for the evaluation harness itself.

A grader that silently mis-scores is worse than no grader, because it produces
confident green. These check the scoring logic, and — most importantly — that
every failure mode of the harness reports FAIL rather than PASS.
"""

from __future__ import annotations

import yaml

from app.providers.base import ProviderResult, Usage
from app.router import Router
from eval.judges import (
    check_contains,
    check_forbids,
    check_judge,
    check_json_paths,
    check_similarity,
    check_tool_arguments,
    check_tool_calls,
    lexical_similarity,
    resolve_path,
)
from eval.runner import (
    GOLDEN_SET,
    CaseResult,
    run_offline_retrieval_case,
    run_offline_tool_case,
)
from tests.conftest import FakeProvider


def _answer(text: str) -> ProviderResult:
    return ProviderResult(
        text=text, upstream_model="m", usage=Usage(1, 1), finish_reason="stop"
    )


# --- the golden set is well formed ----------------------------------------


def test_golden_set_parses_and_every_case_is_complete():
    spec = yaml.safe_load(GOLDEN_SET.read_text(encoding="utf-8"))

    ids = [c["id"] for c in spec["cases"]]
    assert len(ids) == len(set(ids)), "duplicate case ids"

    for case in spec["cases"]:
        assert case["kind"] in {"offline_tool", "offline_retrieval", "online_agent"}
        assert case.get("expect"), f"{case['id']} asserts nothing"


def test_every_online_case_has_at_least_one_decidable_check():
    """The core anti-flake rule: a case anchored ONLY on a judge or a similarity
    score can fail on a model's mood. Every case must have something
    deterministic to stand on."""
    spec = yaml.safe_load(GOLDEN_SET.read_text(encoding="utf-8"))
    decidable = {"tool_calls", "tool_arguments", "contains", "forbids", "delegates"}

    for case in spec["cases"]:
        if case["kind"] != "online_agent":
            continue
        keys = set(case["expect"])
        assert keys & decidable, (
            f"{case['id']} relies only on fuzzy checks {keys}"
        )


# --- deterministic scoring -------------------------------------------------


def test_resolve_path_walks_nested_output():
    data = {"readings": {"temperature": {"value": 87.4}}}
    assert resolve_path(data, "readings.temperature.value") == 87.4


def test_resolve_path_returns_none_for_a_missing_path():
    assert resolve_path({"a": 1}, "a.b.c") is None


def test_json_path_check_fails_on_a_wrong_value():
    checks = check_json_paths({"status": "normal"}, {"status": "alarm"})
    assert checks[0].passed is False
    assert "alarm" in checks[0].detail


def test_contains_is_case_insensitive():
    assert check_contains("Temperature is 87.4 degC", ["87.4", "DEGC"])[1].passed


def test_forbids_catches_a_fabricated_reading():
    """The strongest hallucination check in the set, and fully decidable."""
    checks = check_forbids("Pump 47 is running at 65 degC.", ["degC"])
    assert checks[0].passed is False
    assert "FORBIDDEN" in checks[0].detail


def test_forbids_passes_when_the_model_correctly_declines():
    checks = check_forbids("Pump 47 is not a known asset.", ["degC"])
    assert checks[0].passed is True


def test_tool_call_check_allows_extra_calls_but_not_missing_ones():
    """An agent that checks one extra thing has not regressed; one that skips a
    required lookup has."""
    assert check_tool_calls(["get_telemetry", "list_assets"], ["get_telemetry"])[0].passed
    assert not check_tool_calls(["list_assets"], ["get_telemetry"])[0].passed


def test_tool_argument_check_is_a_subset_match():
    calls = [{"tool": "search_manuals", "arguments": {"query": "pump", "max_results": 3}}]
    # Extra arguments are fine...
    assert check_tool_arguments(calls, {"search_manuals": {"query": "pump"}})[0].passed
    # ...a wrong value is not.
    assert not check_tool_arguments(calls, {"search_manuals": {"query": "valve"}})[0].passed


def test_tool_argument_check_fails_clearly_when_the_tool_never_ran():
    checks = check_tool_arguments([], {"get_telemetry": {"asset_id": "pump-3"}})
    assert checks[0].passed is False
    assert "not called" in checks[0].detail


def test_lexical_similarity_scores_overlap_not_meaning():
    """Documents the metric's real limitation: it rewards shared words, so a
    contradiction that reuses the vocabulary scores high. That is why every
    similarity case also carries a judge rubric."""
    reference = "Pump 3 is at 87.4 degC, above the 80 degC limit."
    assert lexical_similarity(reference, reference) == 1.0
    assert lexical_similarity("Something entirely unrelated.", reference) < 0.15
    # A negation scores high despite meaning the opposite — the known weakness.
    contradiction = "Pump 3 is not at 87.4 degC and not above the 80 degC limit."
    assert lexical_similarity(contradiction, reference) > 0.5


def test_similarity_check_reports_the_score_and_threshold():
    check = check_similarity("a b c", "a b c d", 0.5)
    assert check.passed is True
    assert "threshold" in check.detail


# --- the judge fails safe --------------------------------------------------


async def test_judge_parses_a_clean_verdict():
    provider = FakeProvider(
        "fake", ("claude-haiku-4-5",), result=_answer('{"pass": true, "reason": "ok"}')
    )
    check = await check_judge("answer", "rubric", Router([provider]))
    assert check.passed is True
    assert check.detail == "ok"


async def test_judge_reports_a_failure_verdict():
    provider = FakeProvider(
        "fake",
        ("claude-haiku-4-5",),
        result=_answer('{"pass": false, "reason": "invented a reading"}'),
    )
    check = await check_judge("answer", "rubric", Router([provider]))
    assert check.passed is False
    assert "invented" in check.detail


async def test_judge_tolerates_prose_around_the_json():
    provider = FakeProvider(
        "fake",
        ("claude-haiku-4-5",),
        result=_answer('Sure!\n```json\n{"pass": true, "reason": "fine"}\n```'),
    )
    assert (await check_judge("a", "r", Router([provider]))).passed is True


async def test_unparseable_verdict_FAILS_rather_than_passes():
    """An eval that passes when its grader misbehaves is worse than no eval."""
    provider = FakeProvider("fake", ("claude-haiku-4-5",), result=_answer("looks good!"))
    check = await check_judge("a", "r", Router([provider]))
    assert check.passed is False
    assert "unparseable" in check.detail


async def test_unavailable_judge_FAILS_rather_than_passes():
    """No provider configured must not quietly become a green check."""
    check = await check_judge("a", "r", Router([]))
    assert check.passed is False
    assert "unavailable" in check.detail


# --- offline case execution ------------------------------------------------


def test_offline_tool_case_runs_against_the_real_tool():
    case = {
        "id": "t",
        "kind": "offline_tool",
        "tool": "get_telemetry",
        "arguments": {"asset_id": "pump-3"},
        "expect": {"json_path_equals": {"status": "warning"}},
    }
    assert run_offline_tool_case(case).passed is True


def test_offline_retrieval_case_runs_against_the_real_index():
    case = {
        "id": "r",
        "kind": "offline_retrieval",
        "query": "pump bearing temperature limit",
        "expect": {"top_section": "PMP-4.2"},
    }
    assert run_offline_retrieval_case(case).passed is True


def test_a_skipped_case_is_not_a_passed_case():
    """Counting a skip as a pass is how an eval becomes decorative."""
    result = CaseResult("x", "online_agent", skipped=True, skip_reason="no key")

    assert result.status == "SKIP"
    assert result.passed is False


def test_an_errored_case_is_distinct_from_a_failed_one():
    """The system breaking and the system behaving wrongly want different
    investigation."""
    result = CaseResult("x", "online_agent", error="boom")

    assert result.status == "ERROR"
    assert result.passed is False


# --- the retrieval regressions the eval originally caught -----------------


def test_stopwords_do_not_outrank_domain_terms():
    """Regression: 'do' had df=1, so IDF scored it as highly informative and
    'how do I restart...' matched whichever section said 'Do not'."""
    from mcp_server.tools.manuals import search_manuals

    assert search_manuals("how do I restart a feedwater pump")["results"][0][
        "section_id"
    ] == "PMP-7.1"


def test_one_incidental_term_overlap_is_not_a_match():
    """Regression: 'alignment' appears in PMP-4.5, so an unrelated query
    matched it on a single word."""
    from mcp_server.tools.manuals import search_manuals

    assert search_manuals("quantum flux capacitor alignment")["results"] == []


def test_a_titled_section_beats_a_passing_mention():
    """Regression: set-based scoring made 'restart procedure' (titled, mentioned
    three times) tie with a section saying 'Do not restart' once, and the tie
    broke alphabetically."""
    from mcp_server.tools.manuals import _content_tokens, _score, CORPUS

    tokens = _content_tokens("restart feedwater pump")
    by_id = {s.section_id: s for s in CORPUS}

    assert _score(tokens, by_id["PMP-7.1"])[0] > _score(tokens, by_id["PMP-4.2"])[0]
