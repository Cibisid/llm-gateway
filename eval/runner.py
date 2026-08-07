"""The evaluation harness — does this system still behave correctly?

READ THIS FILE. It is one of the four you must be able to explain cold, and it
is the part of the project that most distinguishes it: unit tests prove the code
does what it says, this proves the *system* still does what it is for.

    python -m eval.runner            # run everything available
    python -m eval.runner --offline  # deterministic checks only
    python -m eval.runner --case agent-multi-step-grounded-diagnosis

WHAT IT SCORES
    tool-call correctness  did the right tool fire, with the right arguments?
    grounding              is the actual value present; is a fabricated one absent?
    similarity             lexical floor against answer collapse (see judges.py)
    LLM-as-judge           rubric grading for meaning nothing else can check

THE CENTRAL DESIGN PROBLEM: NON-DETERMINISM
    You cannot assert equality against a model's output — it changes between
    runs, and a suite that fails on harmless rewording gets muted within a week.
    A muted eval is worse than none, because it produces confident green.

    So every case is anchored by at least one DECIDABLE check (an exact tool
    argument, a required substring, a forbidden substring) and the fuzzy checks
    only ADD signal on top. `forbids` is the strongest of these: asserting that
    an answer about a non-existent pump contains no temperature unit is fully
    decidable, needs no model, and catches the failure that actually matters.

THE SECOND DESIGN PROBLEM: CI WITHOUT KEYS
    Model-dependent cases need ANTHROPIC_API_KEY. A fork's CI does not have one.
    The suite splits offline (tools, retrieval) from online (model behaviour),
    always runs offline, and reports skipped online cases as SKIPPED — never as
    passed. Counting a skip as a pass is how an eval becomes decorative.

EXIT CODES — the whole point of running this in CI
    0  everything that ran, passed
    1  at least one check failed
    2  the harness itself could not run (bad golden set, no providers)

    A skip alone never fails the build; `--require-online` makes it fail, for
    the pipeline where a key IS configured and a silent skip would mean the
    model checks quietly stopped running.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import MODEL_ALIASES, get_settings
from app.mcp_client import MCPToolbox
from app.orchestrator import Orchestrator
from app.providers.registry import build_providers
from app.router import Router
from app.schemas import ChatCompletionRequest, ChatMessage
from app.tool_loop import run_tool_loop
from eval.judges import (
    CheckResult,
    check_contains,
    check_forbids,
    check_json_paths,
    check_judge,
    check_similarity,
    check_tool_arguments,
    check_tool_calls,
)
from mcp_server.server import server as mcp_app
from mcp_server.tools.manuals import search_manuals
from mcp_server.tools.telemetry import get_telemetry

GOLDEN_SET = Path(__file__).parent / "golden_set.yaml"

#: Model under test. Sonnet rather than Haiku: the eval should exercise the tier
#: the system is actually expected to run on for agentic work.
EVAL_MODEL = "claude-sonnet-5"


@dataclass
class CaseResult:
    case_id: str
    kind: str
    checks: list[CheckResult] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    error: str | None = None
    duration_s: float = 0.0
    cost_usd: float | None = None

    @property
    def passed(self) -> bool:
        return (
            not self.skipped
            and self.error is None
            and all(c.passed for c in self.checks)
        )

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.error:
            return "ERROR"
        return "PASS" if self.passed else "FAIL"


# --- offline cases: no model, no key --------------------------------------


def run_offline_tool_case(case: dict) -> CaseResult:
    result = CaseResult(case["id"], case["kind"])
    started = time.perf_counter()

    output = get_telemetry(**case["arguments"])
    expect = case.get("expect", {})

    if "json_path_equals" in expect:
        result.checks.extend(check_json_paths(output, expect["json_path_equals"]))
    if "contains" in expect:
        # Serialised, because the assertion is about what reaches the model.
        result.checks.extend(check_contains(str(output), expect["contains"]))

    result.duration_s = time.perf_counter() - started
    return result


def run_offline_retrieval_case(case: dict) -> CaseResult:
    result = CaseResult(case["id"], case["kind"])
    started = time.perf_counter()

    output = search_manuals(case["query"])
    hits = output["results"]
    expect = case.get("expect", {})

    if expect.get("empty"):
        result.checks.append(
            CheckResult(
                "no_match",
                hits == [],
                "correctly returned nothing"
                if hits == []
                else f"expected no match, got {[h['section_id'] for h in hits]}",
            )
        )
    if "top_section" in expect:
        top = hits[0]["section_id"] if hits else None
        result.checks.append(
            CheckResult(
                "top_section",
                top == expect["top_section"],
                f"expected {expect['top_section']}, got {top}",
            )
        )

    result.duration_s = time.perf_counter() - started
    return result


# --- online cases: need a real model --------------------------------------


async def run_online_case(
    case: dict,
    router: Router,
    toolbox: MCPToolbox,
    orchestrator: Orchestrator,
) -> CaseResult:
    result = CaseResult(case["id"], case["kind"])
    started = time.perf_counter()
    expect = case.get("expect", {})

    request = ChatCompletionRequest(
        model=EVAL_MODEL,
        messages=[ChatMessage(role="user", content=case["prompt"].strip())],
    )

    try:
        if case.get("endpoint") == "agent":
            outcome = await orchestrator.run(request)
            answer = outcome.text
            calls = [
                {"tool": s.tool, "arguments": s.arguments} for s in outcome.steps
            ]
            result.cost_usd = outcome.total_cost_usd
            delegated = outcome.delegated
        else:
            loop = await run_tool_loop(request, router, toolbox)
            answer = loop.text
            calls = [{"tool": s.tool, "arguments": s.arguments} for s in loop.steps]
            result.cost_usd = loop.total_cost_usd
            delegated = False
    except Exception as exc:  # noqa: BLE001
        # An exception is an ERROR, distinct from a FAIL: the system broke
        # rather than behaved wrongly, and those want different investigation.
        result.error = f"{type(exc).__name__}: {exc}"
        result.duration_s = time.perf_counter() - started
        return result

    called = [c["tool"] for c in calls]

    if "tool_calls" in expect:
        result.checks.extend(check_tool_calls(called, expect["tool_calls"]))
    if "tool_arguments" in expect:
        result.checks.extend(check_tool_arguments(calls, expect["tool_arguments"]))
    if "delegates" in expect:
        result.checks.append(
            CheckResult(
                "delegates",
                delegated is bool(expect["delegates"]),
                f"delegated={delegated}, expected {expect['delegates']}",
            )
        )
    if "contains" in expect:
        result.checks.extend(check_contains(answer, expect["contains"]))
    if "forbids" in expect:
        result.checks.extend(check_forbids(answer, expect["forbids"]))
    if "similarity_to" in expect:
        result.checks.append(
            check_similarity(
                answer,
                expect["similarity_to"],
                float(expect.get("similarity_threshold", 0.25)),
            )
        )
    if "judge" in expect:
        result.checks.append(await check_judge(answer, expect["judge"], router))

    result.duration_s = time.perf_counter() - started
    return result


# --- reporting -------------------------------------------------------------


def print_report(results: list[CaseResult], *, verbose: bool) -> None:
    print()
    print("=" * 78)
    print("EVALUATION REPORT")
    print("=" * 78)

    for result in results:
        marker = {"PASS": "  ok  ", "FAIL": " FAIL ", "SKIP": " skip ", "ERROR": "ERROR "}[
            result.status
        ]
        cost = f" ${result.cost_usd:.5f}" if result.cost_usd else ""
        print(f"[{marker}] {result.case_id}  ({result.duration_s:.2f}s{cost})")

        if result.skipped:
            print(f"           reason: {result.skip_reason}")
            continue
        if result.error:
            print(f"           error: {result.error}")
            continue

        # Failures always print their detail; passes only under --verbose.
        # A report nobody can read is a report nobody reads.
        for check in result.checks:
            if not check.passed or verbose:
                print(f"           {check.symbol} {check.name}: {check.detail}")

    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    errored = sum(1 for r in results if r.status == "ERROR")
    skipped = sum(1 for r in results if r.status == "SKIP")
    total_cost = sum(r.cost_usd or 0.0 for r in results)

    print("-" * 78)
    print(
        f"{passed} passed, {failed} failed, {errored} errored, {skipped} skipped "
        f"of {len(results)} cases"
    )
    if total_cost:
        print(f"total model cost: ${total_cost:.5f}")
    if skipped:
        print(
            f"\nNOTE: {skipped} case(s) were SKIPPED, not passed. "
            "Set ANTHROPIC_API_KEY to run the model-dependent checks."
        )
    print("=" * 78)


# --- entry point -----------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    try:
        spec: dict[str, Any] = yaml.safe_load(GOLDEN_SET.read_text(encoding="utf-8"))
        cases = spec["cases"]
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot load golden set: {exc}", file=sys.stderr)
        return 2

    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
        if not cases:
            print(f"No case matched {args.case}", file=sys.stderr)
            return 2

    settings = get_settings()
    providers = build_providers(settings)
    have_model = bool(providers) and not args.offline

    results: list[CaseResult] = []
    offline_cases = [c for c in cases if c["kind"].startswith("offline")]
    online_cases = [c for c in cases if not c["kind"].startswith("offline")]

    for case in offline_cases:
        if case["kind"] == "offline_tool":
            results.append(run_offline_tool_case(case))
        else:
            results.append(run_offline_retrieval_case(case))

    if online_cases:
        if not have_model:
            reason = (
                "--offline requested"
                if args.offline
                else "no provider configured (set ANTHROPIC_API_KEY)"
            )
            for case in online_cases:
                results.append(
                    CaseResult(case["id"], case["kind"], skipped=True, skip_reason=reason)
                )
        else:
            router = Router(
                providers, strategy=settings.routing_strategy, aliases=MODEL_ALIASES
            )
            async with MCPToolbox(mcp_app) as toolbox:
                orchestrator = Orchestrator(
                    router, toolbox, specialist_url="", http_client=None
                )
                for case in online_cases:
                    results.append(
                        await run_online_case(case, router, toolbox, orchestrator)
                    )

    print_report(results, verbose=args.verbose)

    if any(r.status in {"FAIL", "ERROR"} for r in results):
        return 1
    if args.require_online and any(r.status == "SKIP" for r in results):
        print(
            "FAILING: --require-online was set but model cases were skipped.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the golden-set evaluation.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run only deterministic checks; never call a model.",
    )
    parser.add_argument(
        "--case", action="append", help="Run only this case id (repeatable)."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show passing checks too."
    )
    parser.add_argument(
        "--require-online",
        action="store_true",
        help="Fail if model cases were skipped (for pipelines that have a key).",
    )
    args = parser.parse_args()

    # Surfaced up front so a run that silently did half the work is obvious
    # from the first line of output rather than the last.
    if not args.offline and not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set — model-dependent cases will be skipped.")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
