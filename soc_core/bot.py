from __future__ import annotations

from dataclasses import dataclass
import html
import re
from datetime import UTC, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message as TgMessage

from soc_core.database import Database
from soc_core.models import AssetType, DispatchMessage, RiskLevel


DETAILS_CB_PREFIX = "details:"
ASSET_CB_PREFIX = "asset:"
ASSET_PAGE_SIZE = 10
ASSET_HISTORY_PAGE_SIZE = 10


def _details_kb(email_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подробности", callback_data=f"{DETAILS_CB_PREFIX}{email_id}")]
        ]
    )


def _icon(level: RiskLevel) -> str:
    if level in (RiskLevel.CRITICAL, RiskLevel.HIGH):
        return "🔴"
    if level == RiskLevel.MEDIUM:
        return "🟡"
    return "🟢"


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _assets_list_text(*, total: int, page: int, pages: int) -> str:
    return (
        "Assets manager\n"
        f"Total: {total} | Page: {page + 1}/{pages}\n\n"
        "Tap a host to classify it as SERVER / WORKSTATION or delete it.\n"
        "UNCLASSIFIED hosts are shown first."
    )


def _assets_list_kb(items: list[tuple[int, str, AssetType]], *, page: int, pages: int) -> InlineKeyboardMarkup:
    kb: list[list[InlineKeyboardButton]] = []
    for asset_id, hostname, t in items:
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{hostname} ({t.value})",
                    callback_data=f"{ASSET_CB_PREFIX}open:{asset_id}:{page}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅ Prev", callback_data=f"{ASSET_CB_PREFIX}list:{page - 1}")
        )
    nav.append(InlineKeyboardButton(text="🔄 Refresh", callback_data=f"{ASSET_CB_PREFIX}list:{page}"))
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(text="Next ➡", callback_data=f"{ASSET_CB_PREFIX}list:{page + 1}")
        )
    if nav:
        kb.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=kb)


def _asset_manage_text(
    hostname: str,
    t: AssetType,
    *,
    recipients: list[tuple[int, int, str, bool]] | None = None,
) -> str:
    extra = ""
    if t == AssetType.UNCLASSIFIED:
        extra = "\n\nThis host is UNCLASSIFIED. Please set SERVER or WORKSTATION."
    rec = ""
    if recipients:
        lines = []
        for _chat_id, user_id, min_risk, enabled in recipients:
            state = "ON" if enabled else "OFF"
            lines.append(f"- user_id={user_id} | min_risk={min_risk} | {state}")
        rec = "\n\nRecipients (optional):\n" + "\n".join(lines)
    return f"Asset: {hostname}\nType: {t.value}{extra}{rec}"


def _asset_manage_kb(asset_id: int, *, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Mark SERVER",
                    callback_data=f"{ASSET_CB_PREFIX}set:{asset_id}:{AssetType.SERVER.value}:{page}",
                ),
                InlineKeyboardButton(
                    text="✅ Mark WORKSTATION",
                    callback_data=f"{ASSET_CB_PREFIX}set:{asset_id}:{AssetType.WORKSTATION.value}:{page}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📜 History (8d)",
                    callback_data=f"{ASSET_CB_PREFIX}hist:{asset_id}:{page}:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ Bind owner",
                    callback_data=f"{ASSET_CB_PREFIX}bind:{asset_id}:{page}:MEDIUM",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Clear owners",
                    callback_data=f"{ASSET_CB_PREFIX}oclr:{asset_id}:{page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Delete",
                    callback_data=f"{ASSET_CB_PREFIX}delc:{asset_id}:{page}",
                ),
                InlineKeyboardButton(text="⬅ Back", callback_data=f"{ASSET_CB_PREFIX}list:{page}"),
            ],
        ]
    )

## claim flow removed (manual admin binding via /bind)


def _bind_owner_text(hostname: str, *, min_risk: str) -> str:
    return (
        f"Bind owner for host: {hostname}\n"
        f"Min risk: {min_risk}\n\n"
        "Send a message with the owner's Telegram user_id (just digits).\n"
        "Tip: user can /start the bot and copy user_id."
    )


def _bind_owner_kb(asset_id: int, *, page: int, min_risk: str) -> InlineKeyboardMarkup:
    mr = (min_risk or "MEDIUM").upper()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ INFO" if mr == "INFO" else "INFO"),
                    callback_data=f"{ASSET_CB_PREFIX}bind:{asset_id}:{page}:INFO",
                ),
                InlineKeyboardButton(
                    text=("✅ MEDIUM" if mr == "MEDIUM" else "MEDIUM"),
                    callback_data=f"{ASSET_CB_PREFIX}bind:{asset_id}:{page}:MEDIUM",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=("✅ HIGH" if mr == "HIGH" else "HIGH"),
                    callback_data=f"{ASSET_CB_PREFIX}bind:{asset_id}:{page}:HIGH",
                ),
                InlineKeyboardButton(
                    text=("✅ CRITICAL" if mr == "CRITICAL" else "CRITICAL"),
                    callback_data=f"{ASSET_CB_PREFIX}bind:{asset_id}:{page}:CRITICAL",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back to asset",
                    callback_data=f"{ASSET_CB_PREFIX}open:{asset_id}:{page}",
                )
            ],
        ]
    )

def _asset_delete_confirm_kb(asset_id: int, *, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="YES, delete",
                    callback_data=f"{ASSET_CB_PREFIX}del:{asset_id}:{page}",
                ),
                InlineKeyboardButton(text="Cancel", callback_data=f"{ASSET_CB_PREFIX}open:{asset_id}:{page}"),
            ]
        ]
    )

def _history_list_text(hostname: str, *, total: int, page: int, pages: int) -> str:
    return (
        f"History (last 8 days) for: {hostname}\n"
        f"Total sent alerts: {total} | Page: {page + 1}/{pages}\n\n"
        "Tap an item to view the exact Telegram text that was sent then."
    )


def _history_list_kb(
    rows: list[tuple[int, datetime, str, int, str | None]],
    *,
    asset_id: int,
    asset_page: int,
    page: int,
    pages: int,
) -> InlineKeyboardMarkup:
    kb: list[list[InlineKeyboardButton]] = []
    for tg_msg_id, sent_at, risk, email_id, model_used in rows:
        stamp = sent_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        model = (model_used or "-")
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"{stamp} | {risk} | email_id={email_id} | {model}",
                    callback_data=f"{ASSET_CB_PREFIX}histopen:{asset_id}:{asset_page}:{page}:{tg_msg_id}",
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅ Prev",
                callback_data=f"{ASSET_CB_PREFIX}hist:{asset_id}:{asset_page}:{page - 1}",
            )
        )
    nav.append(
        InlineKeyboardButton(
            text="🔄 Refresh",
            callback_data=f"{ASSET_CB_PREFIX}hist:{asset_id}:{asset_page}:{page}",
        )
    )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="Next ➡",
                callback_data=f"{ASSET_CB_PREFIX}hist:{asset_id}:{asset_page}:{page + 1}",
            )
        )
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton(text="⬅ Back to asset", callback_data=f"{ASSET_CB_PREFIX}open:{asset_id}:{asset_page}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def _history_view_kb(*, asset_id: int, asset_page: int, hist_page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅ Back to history",
                    callback_data=f"{ASSET_CB_PREFIX}hist:{asset_id}:{asset_page}:{hist_page}",
                )
            ]
        ]
    )


@dataclass
class BotRuntime:
    bot: Bot
    dp: Dispatcher
    router: Router


def build_bot(
    db: Database,
    token: str,
    *,
    allowed_user_ids: list[int] | None = None,
    admin_user_ids: list[int] | None = None,
    admin_chat_ids: list[int] | None = None,
) -> BotRuntime:
    bot = Bot(token=token)
    router = Router()
    dp = Dispatcher()
    dp.include_router(router)

    # "Simple & safe" mode:
    # - users can always /start to see their user_id (onboarding)
    # - only admins (TELEGRAM_ADMIN_USER_IDS) can manage /assets and view raw emails
    # `allowed_user_ids` is kept for backward compatibility but is not used to block /start.
    allowed_set = set(allowed_user_ids or [])
    admin_set = set(admin_user_ids or []) or allowed_set
    admin_chats = list(admin_chat_ids or [])
    # admin_user_id -> (asset_id, asset_page, min_risk, origin_chat_id, origin_message_id)
    bind_sessions: dict[int, tuple[int, int, str, int, int]] = {}

    def _is_admin(user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id in admin_set

    async def _deny_message(m: TgMessage) -> None:
        await m.answer("Access denied.")

    async def _deny_callback(cb: CallbackQuery) -> None:
        try:
            await cb.answer("Access denied.", show_alert=True)
        except Exception:
            pass

    @router.message(CommandStart())
    async def start(m: TgMessage) -> None:
        uid = m.from_user.id if m.from_user else None
        await m.answer(
            "SOCANA bot is running.\n\n"
            f"Your Telegram ID:\nuser_id={uid}\nchat_id={m.chat.id}\n\n"
            "Commands:\n"
            "- /assets (admin: manage hosts)\n"
            "- /whoami (show your Telegram ids)\n"
        )

    @router.message(Command("whoami"))
    async def whoami(m: TgMessage) -> None:
        uid = m.from_user.id if m.from_user else None
        await m.answer(f"user_id={uid}\nchat_id={m.chat.id}")

    @router.message(Command("bind"))
    async def bind_owner(m: TgMessage) -> None:
        if not _is_admin(m.from_user.id if m.from_user else None):
            await _deny_message(m)
            return
        # /bind HOST USER_ID [MIN_RISK]
        text = (m.text or "").strip()
        parts = text.split()
        if len(parts) < 3:
            await m.answer("Usage: /bind <HOSTNAME> <USER_ID> [INFO|MEDIUM|HIGH|CRITICAL]\nExample: /bind BELYKH-IU-PC 123456789 HIGH")
            return
        hostname = parts[1].strip()
        try:
            user_id = int(parts[2].strip())
        except Exception:
            await m.answer("USER_ID must be an integer. Ask the user to /start and copy user_id.")
            return
        min_risk = (parts[3].strip().upper() if len(parts) >= 4 else "MEDIUM")
        try:
            await db.ensure_asset(hostname)
        except Exception:
            pass
        asset = await db.get_asset_by_hostname(hostname)
        if not asset:
            await m.answer("Host not found.")
            return
        asset_id, hn, _t = asset
        # In private chat with the bot, chat_id == user_id (after /start). We store both.
        await db.upsert_asset_recipient(asset_id=asset_id, chat_id=user_id, user_id=user_id, min_risk=min_risk, enabled=True)
        await m.answer(f"OK: bound host `{hn}` -> owner user_id={user_id} (min_risk={min_risk}).")

    # Capture only non-command text messages (so it won't swallow /assets, /unbind, etc.)
    @router.message(F.text & ~F.text.startswith("/"))
    async def _bind_owner_capture(m: TgMessage) -> None:
        """
        If admin clicked "Bind owner" in /assets card, capture next message as user_id and apply binding.
        This runs before the generic fallback handler (it's defined earlier in file order).
        """
        uid = m.from_user.id if m.from_user else None
        if uid is None:
            return
        if uid not in bind_sessions:
            return
        if not _is_admin(uid):
            bind_sessions.pop(uid, None)
            return
        text = (m.text or "").strip()
        # Ignore commands (admin might type /assets etc)
        if text.startswith("/"):
            return
        mm = re.search(r"\b(\d{5,})\b", text)
        if not mm:
            await m.answer("Please send the owner's Telegram user_id as digits (e.g. 123456789).")
            return
        owner_id = int(mm.group(1))
        asset_id, asset_page, min_risk, origin_chat_id, origin_message_id = bind_sessions.pop(uid)
        asset = await db.get_asset_by_id(asset_id)
        if not asset:
            await m.answer("Host not found (asset deleted?).")
            return
        _, hostname, t = asset
        await db.upsert_asset_recipient(
            asset_id=asset_id,
            chat_id=owner_id,
            user_id=owner_id,
            min_risk=min_risk,
            enabled=True,
        )
        # Update the assets card message (best-effort)
        try:
            recs0 = await db.list_asset_recipients(asset_id=asset_id)
            recs = [(r[1], r[2], r[3], r[4]) for r in recs0]
            await bot.edit_message_text(
                chat_id=origin_chat_id,
                message_id=origin_message_id,
                text=_asset_manage_text(hostname, t, recipients=recs),
                reply_markup=_asset_manage_kb(asset_id, page=asset_page),
            )
        except Exception:
            pass
        await m.answer(f"OK: bound host `{hostname}` -> owner user_id={owner_id} (min_risk={min_risk}).")

    @router.message(Command("unbind"))
    async def unbind_owner(m: TgMessage) -> None:
        if not _is_admin(m.from_user.id if m.from_user else None):
            await _deny_message(m)
            return
        # /unbind HOST (clears all owners for host)
        text = (m.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await m.answer("Usage: /unbind <HOSTNAME>\nExample: /unbind BELYKH-IU-PC")
            return
        hostname = parts[1].strip()
        asset = await db.get_asset_by_hostname(hostname)
        if not asset:
            await m.answer("Host not found.")
            return
        asset_id, hn, _t = asset
        n = await db.clear_asset_recipients(asset_id=asset_id)
        await m.answer(f"OK: cleared {n} owner(s) for host `{hn}`.")

    @router.message(Command("add_asset"))
    async def add_asset(m: TgMessage) -> None:
        if not _is_admin(m.from_user.id if m.from_user else None):
            await _deny_message(m)
            return
        await m.answer("This command is deprecated. Use /assets to classify hosts.")

    @router.message(Command("remove_asset"))
    async def remove_asset(m: TgMessage) -> None:
        if not _is_admin(m.from_user.id if m.from_user else None):
            await _deny_message(m)
            return
        await m.answer("This command is deprecated. Use /assets to delete hosts.")

    async def _send_assets_list(m: TgMessage, *, page: int = 0) -> None:
        if not _is_admin(m.from_user.id if m.from_user else None):
            await _deny_message(m)
            return
        all_items = await db.list_assets_detailed()
        total = len(all_items)
        if total == 0:
            await m.answer("No assets yet. They will be auto-added on first seen events.")
            return
        pages = max(1, (total + ASSET_PAGE_SIZE - 1) // ASSET_PAGE_SIZE)
        page = _clamp(page, 0, pages - 1)
        start = page * ASSET_PAGE_SIZE
        chunk = all_items[start : start + ASSET_PAGE_SIZE]
        await m.answer(
            _assets_list_text(total=total, page=page, pages=pages),
            reply_markup=_assets_list_kb(chunk, page=page, pages=pages),
        )

    @router.message(Command("assets"))
    async def assets(m: TgMessage) -> None:
        await _send_assets_list(m, page=0)

    @router.message(Command("list_assets"))
    async def list_assets(m: TgMessage) -> None:
        # Backward compatible alias.
        await _send_assets_list(m, page=0)

    @router.message()
    async def fallback(m: TgMessage) -> None:
        # Helps avoid 'Update is not handled' in logs for arbitrary messages.
        # If admin is in "bind owner" flow, don't spam fallback messages.
        uid = m.from_user.id if m.from_user else None
        if uid is not None and uid in bind_sessions:
            return
        await m.answer(
            "Unknown command. Use:\n"
            "- /assets\n"
            "- /whoami\n"
        )

    async def _edit_assets_list(cb: CallbackQuery, *, page: int) -> None:
        all_items = await db.list_assets_detailed()
        total = len(all_items)
        pages = max(1, (total + ASSET_PAGE_SIZE - 1) // ASSET_PAGE_SIZE)
        page = _clamp(page, 0, pages - 1)
        start = page * ASSET_PAGE_SIZE
        chunk = all_items[start : start + ASSET_PAGE_SIZE]
        if cb.message:
            await cb.message.edit_text(
                _assets_list_text(total=total, page=page, pages=pages),
                reply_markup=_assets_list_kb(chunk, page=page, pages=pages),
            )

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}list:"))
    async def assets_list_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        # leaving bind mode
        if cb.from_user:
            bind_sessions.pop(cb.from_user.id, None)
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        # asset:list:<page>
        try:
            page = int(parts[2]) if len(parts) >= 3 else 0
        except Exception:
            page = 0
        await _edit_assets_list(cb, page=page)

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}open:"))
    async def asset_open_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        # leaving bind mode
        if cb.from_user:
            bind_sessions.pop(cb.from_user.id, None)
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        # asset:open:<id>:<page>
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        page = int(parts[3]) if len(parts) >= 4 else 0
        item = await db.get_asset_by_id(asset_id)
        if not item:
            if cb.message:
                await cb.message.edit_text("Not found", reply_markup=_assets_list_kb([], page=page, pages=1))
            return
        _, hostname, t = item
        recs0 = await db.list_asset_recipients(asset_id=asset_id)
        recs = [(r[1], r[2], r[3], r[4]) for r in recs0]
        if cb.message:
            await cb.message.edit_text(
                _asset_manage_text(hostname, t, recipients=recs),
                reply_markup=_asset_manage_kb(asset_id, page=page),
            )

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}bind:"))
    async def asset_bind_owner_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        try:
            await cb.answer()
        except Exception:
            pass
        if not cb.from_user or not cb.message:
            return
        parts = (cb.data or "").split(":")
        # asset:bind:<asset_id>:<asset_page>:<min_risk>
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        asset_page = int(parts[3]) if len(parts) >= 4 else 0
        min_risk = (parts[4] if len(parts) >= 5 else "MEDIUM").strip().upper()
        if min_risk not in ("INFO", "MEDIUM", "HIGH", "CRITICAL"):
            min_risk = "MEDIUM"
        asset = await db.get_asset_by_id(asset_id)
        if not asset:
            await cb.message.edit_text("Not found")
            return
        _, hostname, _t = asset
        bind_sessions[cb.from_user.id] = (asset_id, asset_page, min_risk, cb.message.chat.id, cb.message.message_id)
        await cb.message.edit_text(
            _bind_owner_text(hostname, min_risk=min_risk),
            reply_markup=_bind_owner_kb(asset_id, page=asset_page, min_risk=min_risk),
        )

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}set:"))
    async def asset_set_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        # asset:set:<id>:<type>:<page>
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        t_raw = parts[3] if len(parts) >= 4 else ""
        page = int(parts[4]) if len(parts) >= 5 else 0
        try:
            t = AssetType(t_raw)
        except Exception:
            t = AssetType.UNCLASSIFIED
        if t == AssetType.UNCLASSIFIED:
            # We don't allow setting back to UNCLASSIFIED via UI.
            if cb.message:
                await cb.message.answer("Type must be SERVER or WORKSTATION.")
            return
        await db.set_asset_type_by_id(asset_id, t)
        item = await db.get_asset_by_id(asset_id)
        if not item:
            await _edit_assets_list(cb, page=page)
            return
        _, hostname, t2 = item
        recs0 = await db.list_asset_recipients(asset_id=asset_id)
        recs = [(r[1], r[2], r[3], r[4]) for r in recs0]
        if cb.message:
            await cb.message.edit_text(
                _asset_manage_text(hostname, t2, recipients=recs),
                reply_markup=_asset_manage_kb(asset_id, page=page),
            )

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}oclr:"))
    async def asset_owners_clear_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        page = int(parts[3]) if len(parts) >= 4 else 0
        await db.clear_asset_recipients(asset_id=asset_id)
        item = await db.get_asset_by_id(asset_id)
        if not item:
            await _edit_assets_list(cb, page=page)
            return
        _, hostname, t = item
        if cb.message:
            await cb.message.edit_text(
                _asset_manage_text(hostname, t, recipients=[]),
                reply_markup=_asset_manage_kb(asset_id, page=page),
            )

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}delc:"))
    async def asset_delete_confirm_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        page = int(parts[3]) if len(parts) >= 4 else 0
        item = await db.get_asset_by_id(asset_id)
        if not item:
            await _edit_assets_list(cb, page=page)
            return
        _, hostname, t = item
        if cb.message:
            await cb.message.edit_text(
                f"Delete asset?\n\n{hostname} ({t.value})",
                reply_markup=_asset_delete_confirm_kb(asset_id, page=page),
            )

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}del:"))
    async def asset_delete_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        page = int(parts[3]) if len(parts) >= 4 else 0
        await db.delete_asset_by_id(asset_id)
        await _edit_assets_list(cb, page=page)

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}hist:"))
    async def asset_history_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        # asset:hist:<asset_id>:<asset_page>:<hist_page>
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        asset_page = int(parts[3]) if len(parts) >= 4 else 0
        hist_page = int(parts[4]) if len(parts) >= 5 else 0

        asset = await db.get_asset_by_id(asset_id)
        if not asset:
            if cb.message:
                await cb.message.edit_text("Not found")
            return
        _, hostname, _t = asset

        since = datetime.now(tz=UTC) - timedelta(days=8)
        total = await db.count_telegram_history_for_device(device=hostname, since_utc=since)
        pages = max(1, (total + ASSET_HISTORY_PAGE_SIZE - 1) // ASSET_HISTORY_PAGE_SIZE)
        hist_page = _clamp(hist_page, 0, pages - 1)
        rows = await db.list_telegram_history_for_device(
            device=hostname,
            since_utc=since,
            limit=ASSET_HISTORY_PAGE_SIZE,
            offset=hist_page * ASSET_HISTORY_PAGE_SIZE,
        )
        if cb.message:
            await cb.message.edit_text(
                _history_list_text(hostname, total=total, page=hist_page, pages=pages),
                reply_markup=_history_list_kb(
                    rows, asset_id=asset_id, asset_page=asset_page, page=hist_page, pages=pages
                ),
            )

    @router.callback_query(F.data.startswith(f"{ASSET_CB_PREFIX}histopen:"))
    async def asset_history_open_cb(cb: CallbackQuery) -> None:
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        try:
            await cb.answer()
        except Exception:
            pass
        parts = (cb.data or "").split(":")
        # asset:histopen:<asset_id>:<asset_page>:<hist_page>:<tg_msg_id>
        asset_id = int(parts[2]) if len(parts) >= 3 else 0
        asset_page = int(parts[3]) if len(parts) >= 4 else 0
        hist_page = int(parts[4]) if len(parts) >= 5 else 0
        tg_msg_id = int(parts[5]) if len(parts) >= 6 else 0

        res = await db.get_telegram_message_text(tg_msg_id)
        if not res:
            if cb.message:
                await cb.message.edit_text("Not found", reply_markup=_history_view_kb(asset_id=asset_id, asset_page=asset_page, hist_page=hist_page))
            return
        text_sent, email_id = res
        # We stored exact Telegram text that was sent (includes icon + <pre>...</pre>).
        if cb.message:
            await cb.message.edit_text(
                text_sent,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🧾 Raw email", callback_data=f"{DETAILS_CB_PREFIX}{email_id}")],
                        [InlineKeyboardButton(text="⬅ Back to history", callback_data=f"{ASSET_CB_PREFIX}hist:{asset_id}:{asset_page}:{hist_page}")],
                    ]
                ),
            )

    @router.callback_query(F.data.startswith(DETAILS_CB_PREFIX))
    async def details(cb: CallbackQuery) -> None:
        # Raw email is admins-only (safe by default).
        if not _is_admin(cb.from_user.id if cb.from_user else None):
            await _deny_callback(cb)
            return
        # ACK callback ASAP, otherwise Telegram may return:
        # "query is too old and response timeout expired or query ID is invalid"
        try:
            await cb.answer()
        except Exception:
            # best-effort; don't fail the whole handler
            pass

        data = cb.data or ""
        try:
            email_id = int(data.split(":", 1)[1])
        except Exception:
            if cb.message:
                await cb.message.answer("Bad callback")
            return
        raw = await db.get_email_raw_text(email_id)
        if not raw:
            if cb.message:
                await cb.message.answer("Not found")
            return
        # Telegram message size limit: split into chunks
        chunk = 3500
        if not cb.message:
            return
        for i in range(0, len(raw), chunk):
            safe = html.escape(raw[i : i + chunk])
            await cb.message.answer(f"<pre>{safe}</pre>", parse_mode="HTML")

    # claim callbacks removed (manual admin binding flow)

    return BotRuntime(bot=bot, dp=dp, router=router)


async def send_dispatch(
    bot: Bot,
    chat_id: int,
    msg: DispatchMessage,
    *,
    include_details_button: bool = True,
) -> tuple[str, int]:
    # Use HTML to avoid Markdown escaping issues (paths/underscores/backslashes/etc).
    # Wrap content in <pre> to keep monospace formatting for paths and hashes.
    body = html.escape(msg.text)
    text = f"{_icon(msg.risk_level)}\n<pre>{body}</pre>"
    kb = _details_kb(msg.email_id) if include_details_button else None
    m = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)
    return text, m.message_id

