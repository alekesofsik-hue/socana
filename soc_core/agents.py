from __future__ import annotations

import os

from soc_core.models import EnrichedEvent, KasperskyEvent
from soc_core.prompts import Prompts, default_prompts


async def run_crewai_analysis(
    *,
    event: KasperskyEvent,
    enriched: EnrichedEvent,
    servers: list[str],
    repeats: int,
    model: str,
    prompts: Prompts | None = None,
    openai_api_key: str | None = None,
    extra_context: str | None = None,
) -> str:
    """
    CrewAI слой. Держим очень тонкую интеграцию, чтобы при ENABLE_LLM=false
    вся система работала на rules-first.
    """
    try:
        from crewai import Agent, Crew, Task
    except Exception as e:
        # crewai не обязателен для rules-first режима
        raise RuntimeError("CrewAI is not available") from e

    # Ensure provider SDK is configured for CrewAI/LiteLLM
    if openai_api_key:
        os.environ["OPENAI_API_KEY"] = openai_api_key

    servers_ctx = ", ".join(servers) if servers else "(none)"
    p = prompts or default_prompts()

    # LiteLLM generally expects provider prefix; keep as-is if already provided.
    llm_model = model
    if llm_model and "/" not in llm_model:
        llm_model = f"openai/{llm_model}"

    analyst = Agent(
        role=p.analyst.role,
        goal=p.analyst.goal,
        backstory=p.analyst.backstory,
        allow_delegation=False,
        verbose=False,
        llm=llm_model,
    )

    researcher = Agent(
        role=p.researcher.role,
        goal=p.researcher.goal,
        backstory=p.researcher.backstory,
        allow_delegation=False,
        verbose=False,
        llm=llm_model,
    )

    dispatcher = Agent(
        role=p.dispatcher.role,
        goal=p.dispatcher.goal,
        backstory=p.dispatcher.backstory,
        allow_delegation=False,
        verbose=False,
        llm=llm_model,
    )

    base_ctx = f"""
Event:
- vendor_severity: {event.vendor_severity}
- device: {event.device}
- event_type: {event.event_type}
- detection_name: {event.detection_name}
- object_path: {event.object_path}
- process_name: {event.process_name}
- sha256: {event.sha256}
- user: {event.user}
- result: {event.result}
- event_time_utc: {event.event_time}

Context:
- asset_type: {enriched.asset_type}
- rules_risk_level: {enriched.risk_level}
- rules_reason: {enriched.risk_reason}
- repeats_counter: {repeats}
- known_servers: {servers_ctx}
""".strip()

    if extra_context:
        base_ctx = base_ctx + "\n\nExtra context:\n" + extra_context.strip()

    t1 = Task(description=base_ctx + "\n\n" + p.tasks.soc_analysis_suffix, expected_output="Risk assessment + pattern classification", agent=analyst)
    t2 = Task(description=base_ctx + "\n\n" + p.tasks.threat_research_suffix, expected_output="MITRE technique + Kaspersky link", agent=researcher)
    t3 = Task(description=base_ctx + "\n\n" + p.tasks.telegram_report_suffix, expected_output="Telegram-ready text", agent=dispatcher)

    crew = Crew(agents=[analyst, researcher, dispatcher], tasks=[t1, t2, t3], verbose=False)
    result = crew.kickoff()
    return str(result)

