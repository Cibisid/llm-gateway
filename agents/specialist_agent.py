"""The reliability specialist — a second agent, reachable over A2A.

WHY A SEPARATE AGENT RATHER THAN ANOTHER TOOL
    A tool returns data. This returns *judgement*: it runs its own model call
    with its own system prompt, its own model tier, and its own domain framing.
    The orchestrator does not know how it reaches its conclusion, only how to
    ask — which is the entire point of agent-to-agent delegation, and the thing
    that makes it different from adding a fourth MCP tool.

    Concretely, it is separable: it could move to another team's service on
    another host, and the orchestrator would change only its base URL.

WHAT MAKES IT A REAL SPECIALIST
    A different system prompt (failure-mode analysis, not general helpfulness)
    and a deliberately cheaper model tier — delegated sub-analysis does not need
    the orchestrator's reasoning budget. Routing it through the same gateway
    Router means the delegation is cost-tracked like everything else.
"""

from __future__ import annotations

import logging

from app.a2a import AgentCard, AgentSkill
from app.router import Router
from app.schemas import ChatCompletionRequest, ChatMessage

logger = logging.getLogger(__name__)

SPECIALIST_SYSTEM_PROMPT = (
    "You are a rotating-equipment reliability specialist. You are being "
    "consulted by another agent, not by a human, so answer densely and skip "
    "pleasantries.\n\n"
    "Given equipment readings or a described symptom, give: the most likely "
    "failure mode, the physical reasoning behind it, and the single most "
    "informative next check. Be specific about what would confirm or rule out "
    "your hypothesis.\n\n"
    "You do not have tool access. Reason only from what you are given. If the "
    "information is insufficient to name a failure mode, say exactly what "
    "additional reading you would need — do not speculate past the evidence."
)

#: Cheaper tier than the orchestrator on purpose: this is bounded sub-analysis,
#: not open-ended reasoning, and delegation should not cost more than doing the
#: work inline.
SPECIALIST_MODEL = "claude-haiku-4-5"


def agent_card(base_url: str) -> AgentCard:
    """The A2A discovery document other agents read to decide whether to call."""
    return AgentCard(
        name="Reliability Specialist",
        description=(
            "Analyses rotating-equipment telemetry and symptoms to identify "
            "likely failure modes and recommend the next diagnostic step."
        ),
        url=f"{base_url}/a2a/specialist",
        version="0.1.0",
        skills=(
            AgentSkill(
                id="failure-mode-analysis",
                name="Failure mode analysis",
                description=(
                    "Given sensor readings or an observed symptom, identify the "
                    "most likely failure mode and the reasoning behind it."
                ),
                tags=("reliability", "diagnostics", "rotating-equipment"),
                examples=(
                    "Pump 3 bearing is at 87.4 degC with vibration at 4.1 mm/s. "
                    "What is failing?",
                ),
            ),
            AgentSkill(
                id="next-check-recommendation",
                name="Next check recommendation",
                description=(
                    "Recommend the single most informative diagnostic check to "
                    "confirm or rule out a suspected failure mode."
                ),
                tags=("reliability", "diagnostics"),
            ),
        ),
    )


async def handle_task(question: str, router: Router) -> str:
    """Run the specialist's own model call and return its analysis.

    Deliberately a single completion with no tools: the specialist's value is
    judgement over evidence it is handed, and giving it tools would make it a
    second orchestrator rather than a specialist.
    """
    request = ChatCompletionRequest(
        model=SPECIALIST_MODEL,
        messages=[
            ChatMessage(role="system", content=SPECIALIST_SYSTEM_PROMPT),
            ChatMessage(role="user", content=question),
        ],
        max_tokens=1024,
    )

    result, decision = await router.route(request)
    logger.info(
        "specialist answered via %s cost_usd=%s",
        decision.provider,
        decision.cost_usd,
    )
    return result.text
