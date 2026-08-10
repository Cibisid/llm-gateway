"""Ask the agent real questions, against real live data. Costs ~15p.

    python live_demo.py                  # the four scripted questions
    python live_demo.py "your question"  # ask your own

Unlike demo.py, nothing here is faked: a real Claude decides which tools to
call, and the tools call real public APIs. Every number in the output was
fetched while you watched.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.config import MODEL_ALIASES, get_settings
from app.mcp_client import MCPToolbox
from app.providers.registry import build_providers
from app.router import Router
from app.schemas import ChatCompletionRequest, ChatMessage
from app.tool_loop import run_tool_loop
from mcp_server.server import server as mcp_app

# The plumbing logs a lot at INFO. For a demo the tool calls and the answer are
# the story; the routing chatter is noise.
logging.disable(logging.INFO)

SCRIPTED = [
    ("LIVE RIVER DATA",
     "What is the water level of the River Thames at Sandford, and is that normal?"),
    ("LIVE AVIATION WEATHER",
     "What's the weather at Heathrow right now, and is it good flying weather?"),
    ("LIVE EXCHANGE RATES",
     "How many US dollars would I get for 500 pounds today?"),
    ("HALLUCINATION TRAP - the important one",
     "What is the water level of the River Snozzcumber at Fizzlewick?"),
]


async def ask(router, toolbox, label: str, question: str) -> float:
    print("\n" + "=" * 74)
    print(label)
    print("=" * 74)
    print(f'Q: "{question}"\n')

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[ChatMessage(role="user", content=question)],
    )
    result = await run_tool_loop(request, router, toolbox, max_iterations=5)

    if result.steps:
        print("   the model chose to call:")
        for step in result.steps:
            flag = "  [tool reported an error]" if step.is_error else ""
            print(f"     {step.tool}({step.arguments}){flag}")
    else:
        print("   (answered without needing a tool)")

    print(f"\nA: {result.text.strip()}")
    cost = result.total_cost_usd or 0.0
    print(f"\n   [{len(result.steps)} tool calls · ${cost:.5f}]")
    return cost


async def main() -> None:
    settings = get_settings()
    providers = build_providers(settings)
    if not providers:
        print("\nNo provider configured. Put ANTHROPIC_API_KEY in .env first.\n")
        return

    router = Router(providers, strategy="cost", aliases=MODEL_ALIASES)
    questions = (
        [("YOUR QUESTION", " ".join(sys.argv[1:]))] if len(sys.argv) > 1 else SCRIPTED
    )

    print("\nAI GATEWAY — LIVE. Real model, real external data, nothing stubbed.")

    total = 0.0
    async with MCPToolbox(mcp_app) as toolbox:
        print(f"{len(toolbox.specs)} tools available to the model.")
        for label, question in questions:
            total += await ask(router, toolbox, label, question)

    print("\n" + "=" * 74)
    print(f"TOTAL SPEND THIS RUN: ${total:.5f}")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
