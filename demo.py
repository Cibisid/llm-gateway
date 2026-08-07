"""Live walkthrough of the gateway, with a stand-in for the model.

    python demo.py

Everything here is the REAL code path. The only thing faked is the model
itself: `StubModel` returns the tool calls a real Claude would return, so the
whole machine runs end to end without an API key.

Where the real model would be is marked >>> MODEL <<< at each point.
"""

from __future__ import annotations

import asyncio
import textwrap

from app.mcp_client import MCPToolbox
from app.pricing import MODEL_PRICING, compute_cost_usd
from app.providers.base import Provider, ProviderResult, ToolCall, Usage
from app.router import Router
from app.schemas import ChatCompletionRequest, ChatMessage
from app.tool_loop import run_tool_loop
from mcp_server.server import server as mcp_app
from mcp_server.tools.manuals import search_manuals
from mcp_server.tools.telemetry import get_telemetry

BAR = "=" * 74


def head(n: int, title: str) -> None:
    print(f"\n{BAR}\n{n}. {title}\n{BAR}")


def wrap(text: str, indent: str = "   ") -> str:
    return textwrap.indent(textwrap.fill(text, 68), indent)


class StubModel(Provider):
    """Stands in for Claude. Returns a scripted sequence of decisions.

    A real Anthropic call happens in app/providers/anthropic_provider.py.
    This class implements the exact same contract (app/providers/base.py),
    which is why the router, tool loop, and cost tracking cannot tell the
    difference.
    """

    name = "stub-model"
    supports_tools = True
    supported_models = ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5")

    def __init__(self, script):
        self._script = list(script)
        self.seen_tools = []

    async def chat(self, request, *, tools=(), extra_turns=()):
        self.seen_tools.append([t.name for t in tools])
        return self._script.pop(0)


async def main() -> None:
    print("\nAI GATEWAY - LIVE WALKTHROUGH")
    print("Real code, real tools, real retrieval. Model is stubbed.\n")

    # ---------------------------------------------------------------- 1
    head(1, "THE TOOLS ARE REAL  (no model involved)")
    telemetry = get_telemetry("pump-3")
    print(f"   get_telemetry('pump-3')")
    print(f"     status            {telemetry['status']}")
    for name, r in telemetry["readings"].items():
        flag = "  <-- BREACHED" if r["breached"] else ""
        print(f"     {name:<18} {r['value']:>7} {r['unit']:<6} limit {r['high_limit']}{flag}")
    print(f"\n   Status is DERIVED from the readings, never stored -")
    print(f"   a status that can disagree with its own data is worse than none.")

    # ---------------------------------------------------------------- 2
    head(2, "RETRIEVAL IS REAL  (this is the RAG)")
    hits = search_manuals("pump bearing temperature limit")["results"]
    for h in hits[:2]:
        print(f"   [{h['section_id']}] {h['title']}   (relevance {h['relevance']})")
    print()
    print(wrap(hits[0]["text"]))
    print(f"\n   Every result carries a section_id so a claim can be traced.")
    print(f"   Grounding without traceability is a nicer-sounding guess.")

    # ---------------------------------------------------------------- 3
    head(3, "THE ROUTER PICKS A MODEL  (cost strategy)")
    print("   Verified prices (USD per million tokens):")
    for model, p in MODEL_PRICING.items():
        print(f"     {model:<20} in ${float(p.input_usd_per_mtok):>6.2f}   out ${float(p.output_usd_per_mtok):>6.2f}")
    print("     gpt-4o               UNPRICED -> sorts LAST, never treated as free")

    stub = StubModel([])
    router = Router(
        [stub],
        strategy="cost",
        aliases={"auto": ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")},
    )
    print("\n   Client asks for model='auto'. Router expands and ranks:")
    for i, c in enumerate(router.select_candidates("auto"), 1):
        print(f"     {i}. {c.model}")
    print("\n   Config listed opus first; cost ranking reversed it. The client's")
    print("   request never changed - only the gateway's policy.")

    # ---------------------------------------------------------------- 4
    head(4, "COST IS COMPUTED FROM REAL TOKEN COUNTS")
    usage = Usage(input_tokens=1_240, output_tokens=380)
    for model in ("claude-opus-5", "claude-haiku-4-5"):
        cost = compute_cost_usd(model, usage)
        print(f"   {model:<20} {usage.input_tokens} in + {usage.output_tokens} out  ->  ${cost:.8f}")
    print(f"   gpt-4o               -> {compute_cost_usd('gpt-4o', usage)}   (unpriced, not guessed)")

    # ---------------------------------------------------------------- 5
    head(5, "THE FULL AGENT LOOP  (>>> MODEL <<< is stubbed here)")

    async with MCPToolbox(mcp_app) as toolbox:
        print("   Tools discovered over MCP:", [s.name for s in toolbox.specs])

        script = [
            # >>> MODEL <<< turn 1: real Claude would decide this
            ProviderResult(
                text="", upstream_model="claude-sonnet-5", usage=Usage(820, 55),
                finish_reason="tool_calls",
                tool_calls=(ToolCall("c1", "get_telemetry", {"asset_id": "pump-3"}),),
            ),
            # >>> MODEL <<< turn 2
            ProviderResult(
                text="", upstream_model="claude-sonnet-5", usage=Usage(1150, 48),
                finish_reason="tool_calls",
                tool_calls=(ToolCall("c2", "search_manuals",
                                     {"query": "pump bearing temperature limit"}),),
            ),
            # >>> MODEL <<< turn 3: the grounded answer
            ProviderResult(
                text=(
                    "Pump 3's bearing is at 87.4 degC, above its 80 degC limit "
                    "(manual PMP-4.2). Reduce load to 60 percent and verify the "
                    "lubrication oil level. If it reaches 95 degC, shut down and "
                    "isolate immediately."
                ),
                upstream_model="claude-sonnet-5", usage=Usage(1490, 96),
                finish_reason="stop",
            ),
        ]
        loop_router = Router([StubModel(script)], strategy="cost")
        request = ChatCompletionRequest(
            model="claude-sonnet-5",
            messages=[ChatMessage(
                role="user",
                content="Is pump 3 running too hot, and what does the manual say to do?",
            )],
        )

        print(f'\n   USER: "{request.messages[0].content}"\n')
        result = await run_tool_loop(request, loop_router, toolbox)

        for step in result.steps:
            print(f"   >>> MODEL <<< decided: call {step.tool}({step.arguments})")
            first = step.result.strip().splitlines()[0][:60]
            print(f"       GATEWAY ran it -> {first}...")
            print(f"       (this is YOUR code executing, not the model)\n")

        print("   >>> MODEL <<< composed the final answer:\n")
        print(wrap(result.text, "     "))
        print(f"\n   tokens  {result.usage.input_tokens} in / {result.usage.output_tokens} out"
              f"  across {len(result.steps) + 1} model calls")
        print(f"   cost    ${result.total_cost_usd:.8f}   (summed, not just the last call)")

    # ---------------------------------------------------------------- 6
    head(6, "WHERE THE REAL MODEL PLUGS IN")
    print("""   The stub above implements the same contract as the real adapter:

     app/providers/base.py            the contract  (Provider.chat)
     app/providers/anthropic_provider.py   <-- THE REAL MODEL CALL LIVES HERE
         self._client.messages.create(...)      HTTPS -> api.anthropic.com

   The model is not in this repo. It runs on Anthropic's servers. This
   codebase decides WHICH model to call, WHAT tools to offer it, executes
   the tools it asks for, feeds results back, and bills the result.

   Swap StubModel for AnthropicProvider by putting a key in .env - nothing
   else in the loop changes. That is what the contract buys.""")

    print(f"\n{BAR}\n")


if __name__ == "__main__":
    asyncio.run(main())
