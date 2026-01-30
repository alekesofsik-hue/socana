from __future__ import annotations

from datetime import UTC, datetime

from soc_core.database import Database
from soc_core.models import AssetType, DispatchMessage, EnrichedEvent, KasperskyEvent, RiskLevel


def _severity_rank(vendor_severity: str | None) -> int:
    if not vendor_severity:
        return 0
    s = vendor_severity.strip().lower()
    if s in {"critical", "high"}:
        return 3
    if s in {"medium", "moderate"}:
        return 2
    if s in {"low"}:
        return 1
    return 0


def enrich_with_rules(event: KasperskyEvent, asset_type: AssetType | None) -> EnrichedEvent:
    et = (event.event_type or "").lower()
    dn = (event.detection_name or "").lower()

    # 1) Ransomware/Malware/Exploit => CRITICAL
    if any(x in et for x in ("ransomware", "malware", "exploit")) or any(
        x in dn for x in ("ransomware", "exploit")
    ):
        return EnrichedEvent(
            event=event,
            asset_type=asset_type,
            risk_level=RiskLevel.CRITICAL,
            risk_reason="Threat category escalated to CRITICAL (rules)",
        )

    # 2) SERVER + vendor Medium+ => HIGH + note
    if asset_type == AssetType.SERVER and _severity_rank(event.vendor_severity) >= 2:
        return EnrichedEvent(
            event=event,
            asset_type=asset_type,
            risk_level=RiskLevel.HIGH,
            risk_reason="Critical Asset Involved",
        )

    # 2b) UNCLASSIFIED — soft escalation:
    # - High/Critical: keep HIGH, but add note to classify.
    # - Medium: bump to HIGH (without "Critical Asset Involved" wording).
    # - Low/Info: keep scale, but add note to classify.
    if asset_type == AssetType.UNCLASSIFIED:
        sev = _severity_rank(event.vendor_severity)
        if sev >= 3:
            return EnrichedEvent(
                event=event,
                asset_type=asset_type,
                risk_level=RiskLevel.HIGH,
                risk_reason="Asset is UNCLASSIFIED (needs classification)",
            )
        if sev == 2:
            return EnrichedEvent(
                event=event,
                asset_type=asset_type,
                risk_level=RiskLevel.HIGH,
                risk_reason="Soft escalation: UNCLASSIFIED + Medium severity",
            )

    # 3) Standard scale
    sev = _severity_rank(event.vendor_severity)
    if sev >= 3:
        rl = RiskLevel.HIGH
    elif sev == 2:
        rl = RiskLevel.MEDIUM
    elif sev == 1:
        rl = RiskLevel.LOW
    else:
        rl = RiskLevel.INFO

    reason: str | None = None
    if asset_type == AssetType.UNCLASSIFIED:
        reason = "Asset is UNCLASSIFIED (needs classification)"
    return EnrichedEvent(event=event, asset_type=asset_type, risk_level=rl, risk_reason=reason)


def format_rules_summary(enriched: EnrichedEvent, repeats: int) -> str:
    ev = enriched.event
    icon = {
        RiskLevel.CRITICAL: "🔴",
        RiskLevel.HIGH: "🔴",
        RiskLevel.MEDIUM: "🟡",
        RiskLevel.LOW: "🟢",
        RiskLevel.INFO: "🟢",
    }[enriched.risk_level]

    lines: list[str] = []
    lines.append(f"{icon} | {enriched.risk_level.value} | {(ev.event_type or 'Event').strip()} | {(ev.device or 'unknown').strip()}")
    if enriched.asset_type:
        lines.append(f"Asset: {enriched.asset_type.value}")
    if enriched.risk_reason:
        lines.append(f"Reason: {enriched.risk_reason}")
    if ev.detection_name:
        lines.append(f"Detection: {ev.detection_name}")
    if ev.object_path:
        lines.append(f"Object: `{ev.object_path}`")
    if ev.sha256:
        lines.append(f"SHA256: `{ev.sha256}`")
    if ev.user:
        lines.append(f"User: {ev.user}")
    if ev.event_time:
        lines.append(f"Time(UTC): {ev.event_time.astimezone(UTC).isoformat()}")
    if repeats > 1:
        lines.append(f"Repeats (dedup counter): {repeats}")
    return "\n".join(lines)


async def build_dispatch_message(
    db: Database,
    email_id: int,
    event: KasperskyEvent,
    repeats: int,
    enable_llm: bool,
    llm_runner,
) -> DispatchMessage:
    asset_type = await db.get_asset_type(event.device)
    enriched = enrich_with_rules(event, asset_type)

    def _is_retryable_llm_error(e: Exception) -> bool:
        name = type(e).__name__
        msg = str(e)
        # We only retry on connection/timeouts (transient).
        return (
            "APIConnectionError" in name
            or "APIConnectionError" in msg
            or "ConnectionError" in name
            or "ConnectError" in name
            or "Timeout" in name
            or "ReadTimeout" in name
            or "timed out" in msg.lower()
        )

    text: str
    if enable_llm and llm_runner is not None:
        servers = await db.list_servers()
        last_err: Exception | None = None
        # Retry only for transient connection issues (2–3 attempts total).
        for attempt in range(3):
            try:
                text = await llm_runner(event=event, enriched=enriched, servers=servers, repeats=repeats)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if _is_retryable_llm_error(e) and attempt < 2:
                    # simple exponential backoff: 1s, 2s
                    import asyncio

                    await asyncio.sleep(1 * (2**attempt))
                    continue
                text = format_rules_summary(enriched, repeats) + f"\nLLM fallback: {type(e).__name__}"
                break
        else:
            # Should not happen, but keep rules-first safety net.
            if last_err is not None:
                text = format_rules_summary(enriched, repeats) + f"\nLLM fallback: {type(last_err).__name__}"
            else:
                text = format_rules_summary(enriched, repeats)
    else:
        text = format_rules_summary(enriched, repeats)

    return DispatchMessage(text=text, email_id=email_id, risk_level=enriched.risk_level)

