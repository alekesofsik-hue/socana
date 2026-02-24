from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentPrompt:
    role: str
    goal: str
    backstory: str


@dataclass(frozen=True)
class TaskPrompts:
    soc_analysis_suffix: str
    threat_research_suffix: str
    telegram_report_suffix: str
    expected_output_analyst: str
    expected_output_researcher: str
    expected_output_dispatcher: str


@dataclass(frozen=True)
class Prompts:
    analyst: AgentPrompt
    researcher: AgentPrompt
    dispatcher: AgentPrompt
    tasks: TaskPrompts


def default_prompts() -> Prompts:
    return Prompts(
        analyst=AgentPrompt(
            role="Security Analyst (SOC Expert)",
            goal="Assess real risk and detect patterns across events",
            backstory=(
                "Проанализируй данные из парсера и контекст активов из БД. "
                "Определи реальный риск. Если видишь серию атак на разные узлы "
                "(например, AVEVA-7950X и DC-NFS) — классифицируй это как "
                "'Lateral Movement Attempt'."
            ),
        ),
        researcher=AgentPrompt(
            role="Threat Researcher (Intelligence)",
            goal="Provide MITRE ATT&CK technique and Kaspersky references for HIGH/CRITICAL",
            backstory=(
                "Для HIGH/CRITICAL угроз найди код техники в MITRE ATT&CK и краткое описание "
                "зловреда в базах Kaspersky. Дай прямую ссылку."
            ),
        ),
        dispatcher=AgentPrompt(
            role="Telegram Dispatcher",
            goal="Format concise Telegram report",
            backstory=(
                "Сформируй отчет: 🔴/🟡/🟢 | Тип | Устройство. "
                "Используй моноширинный шрифт для путей и хешей."
            ),
        ),
        tasks=TaskPrompts(
            soc_analysis_suffix="SOC analysis:",
            threat_research_suffix="Threat research (only if HIGH/CRITICAL):",
            telegram_report_suffix="Telegram report:",
            expected_output_analyst=(
                "Краткий технический разбор: статус угрозы (Blocked/Active), "
                "уровень риска (LOW/MEDIUM/HIGH/CRITICAL), список затронутых сущностей "
                "и логическое обоснование вердикта (2-3 предложения)."
            ),
            expected_output_researcher=(
                "Код техники MITRE ATT&CK, краткое описание поведения угрозы "
                "и прямые ссылки на Kaspersky Securelist / Encyclopedia. "
                "Если риск LOW/MEDIUM — одна строка: 'Дополнительное исследование не требуется.'"
            ),
            expected_output_dispatcher=(
                "Готовый текст для отправки в Telegram: первая строка "
                "🔴/🟡/🟢 | <RISK> | <event_type> | <device>, "
                "далее блоки Статус, Анализ, Артефакты, Threat Intel, Рекомендации. "
                "Только Markdown, не более 30 строк."
            ),
        ),
    )


def _package_default_path() -> Path:
    return Path(__file__).with_name("prompts.yaml")


def load_prompts(path: str | None) -> Prompts:
    """
    Loads prompts from YAML file. If file is missing/invalid, returns defaults.
    """
    p = Path(path) if path else _package_default_path()
    if not p.exists() or not p.is_file():
        return default_prompts()

    try:
        import yaml  # type: ignore
    except Exception:
        # PyYAML may be missing; don't break runtime
        return default_prompts()

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return default_prompts()

    return _parse_prompts(data, fallback=default_prompts())


def _g(d: dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _parse_prompts(data: dict[str, Any], fallback: Prompts) -> Prompts:
    def _agent(name: str, fb: AgentPrompt) -> AgentPrompt:
        role = _g(data, name, "role") or fb.role
        goal = _g(data, name, "goal") or fb.goal
        backstory = _g(data, name, "backstory") or fb.backstory
        return AgentPrompt(role=str(role), goal=str(goal), backstory=str(backstory))

    def _tasks(fb: TaskPrompts) -> TaskPrompts:
        soc = _g(data, "tasks", "soc_analysis_suffix") or fb.soc_analysis_suffix
        thr = _g(data, "tasks", "threat_research_suffix") or fb.threat_research_suffix
        tg = _g(data, "tasks", "telegram_report_suffix") or fb.telegram_report_suffix
        eo_analyst = _g(data, "tasks", "expected_output_analyst") or fb.expected_output_analyst
        eo_researcher = _g(data, "tasks", "expected_output_researcher") or fb.expected_output_researcher
        eo_dispatcher = _g(data, "tasks", "expected_output_dispatcher") or fb.expected_output_dispatcher
        return TaskPrompts(
            soc_analysis_suffix=str(soc),
            threat_research_suffix=str(thr),
            telegram_report_suffix=str(tg),
            expected_output_analyst=str(eo_analyst),
            expected_output_researcher=str(eo_researcher),
            expected_output_dispatcher=str(eo_dispatcher),
        )

    return Prompts(
        analyst=_agent("analyst", fallback.analyst),
        researcher=_agent("researcher", fallback.researcher),
        dispatcher=_agent("dispatcher", fallback.dispatcher),
        tasks=_tasks(fallback.tasks),
    )

