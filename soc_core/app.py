from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from tenacity import retry, stop_after_attempt, wait_exponential
from aiogram import Bot

from soc_core.agents import run_crewai_analysis
from soc_core.bot import build_bot, send_dispatch
from soc_core.config import Settings
from soc_core.database import Database
from soc_core.imap_client import ImapClient
from soc_core.parser import KasperskyEmailParser
from soc_core.prompts import load_prompts
from soc_core.tools import WebTools
from soc_core.tasks import build_dispatch_message


logger = logging.getLogger("socana")

_RISK_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _dedup_ints(xs: list[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for x in xs:
        try:
            ix = int(x)
        except Exception:
            continue
        if ix in seen:
            continue
        seen.add(ix)
        out.append(ix)
    return out


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=30), reraise=True)
async def _poll_once(
    settings: Settings,
    db: Database,
    *,
    bot: Bot | None = None,
    mode: str = "unseen",
    limit: int = 25,
) -> int:
    imap = ImapClient(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        password=settings.imap_password,
        mailbox=settings.imap_mailbox,
    )
    parser = KasperskyEmailParser()
    logger.info(
        "IMAP poll: host=%s mailbox=%s from=%s",
        settings.imap_host,
        settings.imap_mailbox,
        settings.imap_from_filter,
    )
    if mode == "latest":
        msgs = await imap.fetch_latest_from(settings.imap_from_filter, limit=limit)
    else:
        msgs = await imap.fetch_unseen_from(settings.imap_from_filter, limit=limit)
    logger.info("IMAP poll: found %s messages (mode=%s)", len(msgs), mode)
    if not msgs:
        return 0

    uids_to_mark_seen: list[str] = []
    processed = 0
    for m in msgs:
        parsed = parser.parse(uid=m.uid, raw_email=m.raw)
        logger.debug(
            "Parsed email uid=%s device=%s event_type=%s detection=%s sha256=%s",
            parsed.uid,
            parsed.event.device,
            parsed.event.event_type,
            parsed.event.detection_name,
            parsed.event.sha256,
        )
        email_id, created = await db.upsert_email(
            uid=parsed.uid,
            raw_text=parsed.raw_text,
            message_id=parsed.message_id,
            subject=parsed.subject,
            from_email=parsed.from_email,
            date_utc=parsed.date,
        )
        if not created:
            # Already ingested this UID before.
            # We still may want to deliver it to *newly bound* host owners (replay),
            # without re-running parsing/AI.
            if settings.imap_mark_seen:
                uids_to_mark_seen.append(parsed.uid)
            # Replay to owners if:
            # - we have a stored telegram message for this email_id (already sent to admin earlier)
            # - the owner hasn't received it yet (no telegram_messages row for that chat_id)
            admin_chats = []
            if settings.telegram_chat_id:
                admin_chats.append(settings.telegram_chat_id)
            admin_chats.extend(settings.telegram_admin_chat_ids or [])
            admin_chats = _dedup_ints(admin_chats)
            if not admin_chats:
                continue
            try:
                stored = await db.get_latest_telegram_message_for_email(email_id)
            except Exception:
                stored = None
            if not stored:
                continue
            stored_text, stored_risk = stored
            msg_rank = _RISK_RANK.get((stored_risk or "").upper(), 0)
            try:
                recipients = await db.list_recipients_for_device(device=parsed.event.device)
            except Exception:
                recipients = []
            send_bot = bot
            close_after = False
            if send_bot is None:
                send_bot = Bot(token=settings.telegram_bot_token)
                close_after = True
            for r_chat_id, _r_user_id, min_risk, enabled in recipients:
                if not enabled:
                    continue
                if int(r_chat_id) in set(admin_chats):
                    continue
                mr = (min_risk or "MEDIUM").strip().upper()
                if msg_rank < _RISK_RANK.get(mr, 2):
                    continue
                try:
                    if await db.has_telegram_message(email_id=email_id, chat_id=int(r_chat_id)):
                        continue
                    # Send stored text as-is (HTML). No "Details" button for owners (safe).
                    sent = await send_bot.send_message(
                        chat_id=int(r_chat_id),
                        text=stored_text,
                        parse_mode="HTML",
                    )
                    await db.add_telegram_message(
                        email_id=email_id,
                        device=parsed.event.device,
                        chat_id=int(r_chat_id),
                        telegram_message_id=sent.message_id,
                        sent_at_utc=datetime.now(tz=UTC),
                        risk_level=stored_risk,
                        ai_enabled=settings.enable_llm,
                        model_used=settings.openai_model if settings.enable_llm else None,
                        llm_fallback=None,
                        text_sent=stored_text,
                    )
                except Exception:
                    logger.exception("Replay to owner failed: email_id=%s chat_id=%s", email_id, r_chat_id)
            if close_after:
                try:
                    await send_bot.session.close()
                except Exception:
                    pass
            continue

        # Auto-add host to assets DB on first seen event.
        # New hosts start as UNCLASSIFIED and should be classified later via Telegram bot (/assets).
        try:
            await db.ensure_asset(parsed.event.device)
        except Exception:
            # Best-effort: do not break ingest if assets write fails.
            pass

        await db.insert_event(email_id=email_id, ev=parsed.event)

        now = datetime.now(tz=UTC)
        fp = parsed.event.fingerprint()
        send, repeats = await db.should_send_alert(
            fingerprint=fp,
            now_utc=now,
            window_seconds=settings.anti_spam_window_seconds,
            repeat_threshold=settings.anti_spam_repeat_threshold,
            email_id=email_id,
        )
        logger.info(
            "Dedup: uid=%s send=%s repeats=%s fingerprint=%s",
            parsed.uid,
            send,
            repeats,
            fp[:12] + "...",
        )
        if not send:
            if settings.imap_mark_seen:
                uids_to_mark_seen.append(parsed.uid)
            continue

        prompts = load_prompts(settings.prompts_path)
        web = WebTools(settings.serper_api_key, settings.tavily_api_key)

        async def llm_runner(event, enriched, servers, repeats):
            # Build extra context: web search + simple correlation hints.
            extra: list[str] = []
            sources: list[str] = []

            # Correlation hint: same detection on multiple devices recently.
            since = datetime.now(tz=UTC) - timedelta(hours=2)

            try:
                devices = await db.recent_devices_for_detection(detection_name=event.detection_name, since_utc=since)
                devices = [d for d in devices if d and d != (event.device or "")]
                if devices:
                    extra.append(
                        "Correlation hint: same detection_name seen on other devices recently: "
                        + ", ".join(devices[:10])
                    )
            except Exception:
                pass

            # Web research for HIGH/CRITICAL only
            if getattr(enriched.risk_level, "value", "") in ("HIGH", "CRITICAL"):
                queries = []
                if event.detection_name:
                    queries.append(f"{event.detection_name} Kaspersky")
                    queries.append(f"{event.detection_name} MITRE ATT&CK technique")
                if event.event_type:
                    queries.append(f"{event.event_type} MITRE ATT&CK")

                def _fmt_hits(title_key: str, link_key: str, snippet_key: str, hits: list[dict]) -> str:
                    lines = []
                    for h in hits[:5]:
                        title = (h.get(title_key) or "").strip()
                        link = (h.get(link_key) or "").strip()
                        snippet = (h.get(snippet_key) or "").strip()
                        if not title and not link:
                            continue
                        lines.append(f"- {title} | {link} | {snippet}".strip())
                    return "\n".join(lines)

                for q in queries[:3]:
                    try:
                        serp = web.serper_search(q, num=5)
                        if serp:
                            extra.append("Serper results for: " + q + "\n" + _fmt_hits("title", "link", "snippet", serp))
                            for h in serp[:3]:
                                link = (h.get("link") or "").strip()
                                title = (h.get("title") or "").strip()
                                if link:
                                    sources.append(f"- {title} | {link}")
                    except Exception:
                        pass
                    try:
                        tav = web.tavily_search(q, max_results=5)
                        if tav:
                            extra.append("Tavily results for: " + q + "\n" + _fmt_hits("title", "url", "content", tav))
                            for h in tav[:3]:
                                link = (h.get("url") or "").strip()
                                title = (h.get("title") or "").strip()
                                if link:
                                    sources.append(f"- {title} | {link}")
                    except Exception:
                        pass

            ai_text = await run_crewai_analysis(
                event=event,
                enriched=enriched,
                servers=servers,
                repeats=repeats,
                model=settings.openai_model,
                prompts=prompts,
                openai_api_key=settings.openai_api_key,
                extra_context="\n\n".join(extra) if extra else None,
            )
            # Make AI enrichment visible even if the model output is terse.
            header = f"AI: CrewAI enabled | model={settings.openai_model}"
            if sources:
                header += "\nSources:\n" + "\n".join(sources[:8])
            return header + "\n\n" + ai_text

        dispatch = await build_dispatch_message(
            db=db,
            email_id=email_id,
            event=parsed.event,
            repeats=repeats,
            enable_llm=settings.enable_llm,
            llm_runner=llm_runner if settings.enable_llm else None,
        )

        admin_chats = []
        if settings.telegram_chat_id:
            admin_chats.append(settings.telegram_chat_id)
        admin_chats.extend(settings.telegram_admin_chat_ids or [])
        admin_chats = _dedup_ints(admin_chats)

        if admin_chats:
            send_bot = bot
            close_after = False
            if send_bot is None:
                send_bot = Bot(token=settings.telegram_bot_token)
                close_after = True
            try:
                llm_fallback = None
                if "LLM fallback:" in dispatch.text:
                    # keep short token, e.g. "APIConnectionError"
                    llm_fallback = dispatch.text.split("LLM fallback:", 1)[1].strip().splitlines()[0][:128]

                # 1) Always send to admins (fan-out)
                admin_sent_any = False
                for ac in admin_chats:
                    try:
                        sent_text, tg_message_id = await send_dispatch(
                            send_bot, ac, dispatch, include_details_button=True
                        )
                        admin_sent_any = True
                        await db.add_telegram_message(
                            email_id=email_id,
                            device=parsed.event.device,
                            chat_id=ac,
                            telegram_message_id=tg_message_id,
                            sent_at_utc=now,
                            risk_level=dispatch.risk_level.value,
                            ai_enabled=settings.enable_llm,
                            model_used=settings.openai_model if settings.enable_llm else None,
                            llm_fallback=llm_fallback,
                            text_sent=sent_text,
                        )
                    except Exception:
                        logger.exception("Telegram send failed to admin chat_id=%s for email_id=%s", ac, email_id)

                if not admin_sent_any:
                    raise RuntimeError("Telegram send failed to all admin chats")

                if admin_sent_any:
                    await db.mark_email_telegram_sent(email_id=email_id, when_utc=now)

                # 2) Optional per-host recipients
                try:
                    recipients = await db.list_recipients_for_device(device=parsed.event.device)
                except Exception:
                    recipients = []
                msg_level = str(getattr(dispatch.risk_level, "value", dispatch.risk_level)).upper()
                msg_rank = _RISK_RANK.get(msg_level, 0)
                for r_chat_id, _r_user_id, min_risk, enabled in recipients:
                    if not enabled:
                        continue
                    if int(r_chat_id) in set(admin_chats):
                        continue
                    mr = (min_risk or "MEDIUM").strip().upper()
                    if msg_rank < _RISK_RANK.get(mr, 2):
                        continue
                    try:
                        # Owners receive alerts without raw-email access by default (safe).
                        sent_text, tg_message_id = await send_dispatch(
                            send_bot, int(r_chat_id), dispatch, include_details_button=False
                        )
                        await db.add_telegram_message(
                            email_id=email_id,
                            device=parsed.event.device,
                            chat_id=int(r_chat_id),
                            telegram_message_id=tg_message_id,
                            sent_at_utc=now,
                            risk_level=dispatch.risk_level.value,
                            ai_enabled=settings.enable_llm,
                            model_used=settings.openai_model if settings.enable_llm else None,
                            llm_fallback=llm_fallback,
                            text_sent=sent_text,
                        )
                    except Exception:
                        logger.exception(
                            "Telegram send failed to recipient chat_id=%s for email_id=%s", r_chat_id, email_id
                        )
            except Exception:
                logger.exception("Telegram send failed for email_id=%s (will NOT mark Seen)", email_id)
                # Do not ack IMAP on send failure: allow retry later.
                if close_after:
                    await send_bot.session.close()
                continue
            if close_after:
                await send_bot.session.close()
            logger.info("Telegram: sent message for email_id=%s", email_id)
        else:
            logger.warning("TELEGRAM_CHAT_ID is not set; skipping send")

        if settings.imap_mark_seen:
            uids_to_mark_seen.append(parsed.uid)
        processed += 1

    if settings.imap_mark_seen and uids_to_mark_seen:
        n = await imap.mark_seen_many(uids_to_mark_seen)
        logger.info("IMAP ack: marked_seen=%s", n)

    return processed


async def run(settings: Settings) -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    db = Database(settings.sqlite_path)
    await db.init()

    # Telegram bot runtime (commands/callbacks)
    admin_chats = []
    if settings.telegram_chat_id:
        admin_chats.append(settings.telegram_chat_id)
    admin_chats.extend(settings.telegram_admin_chat_ids or [])
    admin_chats = _dedup_ints(admin_chats)
    runtime = build_bot(
        db=db,
        token=settings.telegram_bot_token,
        allowed_user_ids=settings.telegram_allowed_user_ids,
        admin_user_ids=settings.telegram_admin_user_ids,
        admin_chat_ids=admin_chats,
    )

    async def imap_loop() -> None:
        try:
            while True:
                try:
                    n = await _poll_once(settings, db, bot=runtime.bot)
                    if n:
                        logger.info("Processed %s alerts", n)
                except Exception:
                    logger.exception("IMAP poll failed (will continue)")
                await asyncio.sleep(settings.imap_poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("IMAP loop cancelled")
            raise

    # Run both: aiogram polling + IMAP loop.
    # Important: when systemd sends SIGTERM, aiogram stops polling and returns.
    # We MUST cancel IMAP loop as well, otherwise the process never exits and systemd
    # keeps the service in "deactivating (stop-sigterm)" state.
    polling_task = asyncio.create_task(runtime.dp.start_polling(runtime.bot), name="telegram_polling")
    imap_task = asyncio.create_task(imap_loop(), name="imap_loop")
    try:
        done, pending = await asyncio.wait({polling_task, imap_task}, return_when=asyncio.FIRST_COMPLETED)
        # If polling stops (SIGTERM), cancel IMAP loop and exit.
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # If the completed task raised, re-raise it (keeps logs meaningful).
        for t in done:
            exc = t.exception()
            if exc:
                raise exc
    finally:
        # Best-effort cleanup.
        for t in (polling_task, imap_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(polling_task, imap_task, return_exceptions=True)
        try:
            await runtime.bot.session.close()
        except Exception:
            pass


async def run_once(settings: Settings, *, mode: str = "unseen", limit: int = 25) -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    db = Database(settings.sqlite_path)
    await db.init()
    try:
        bot = Bot(token=settings.telegram_bot_token) if settings.telegram_chat_id else None
        try:
            n = await _poll_once(settings, db, bot=bot, mode=mode, limit=limit)
        finally:
            if bot is not None:
                await bot.session.close()
        logger.info("run-once completed: alerts_processed=%s", n)
    except Exception:
        logger.exception("run-once failed")

