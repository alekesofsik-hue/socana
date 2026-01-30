from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from soc_core.models import AssetType, KasperskyEvent


class Base(DeclarativeBase):
    pass


class AssetORM(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(32), default=AssetType.UNCLASSIFIED.value)  # UNCLASSIFIED/SERVER/WORKSTATION


class AssetRecipientORM(Base):
    __tablename__ = "asset_recipients"
    __table_args__ = (UniqueConstraint("asset_id", "chat_id", name="uix_asset_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)

    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)

    enabled: Mapped[int] = mapped_column(Integer, default=1)
    min_risk: Mapped[str] = mapped_column(String(32), default="MEDIUM")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class EmailORM(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    from_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    telegram_sent: Mapped[int] = mapped_column(Integer, default=0)
    telegram_sent_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["EventORM"]] = relationship(back_populates="email")

    tg_messages: Mapped[list["TelegramMessageORM"]] = relationship(back_populates="email")


class EventORM(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)

    vendor_severity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    event_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detection_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    object_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    process_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_time_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    email: Mapped["EmailORM"] = relationship(back_populates="events")


class DedupORM(Base):
    __tablename__ = "dedup"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer)
    last_alert_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_email_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TelegramMessageORM(Base):
    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    device: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer)

    sent_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    risk_level: Mapped[str] = mapped_column(String(32))

    ai_enabled: Mapped[int] = mapped_column(Integer, default=0)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_fallback: Mapped[str | None] = mapped_column(String(128), nullable=True)

    text_sent: Mapped[str] = mapped_column(Text)

    email: Mapped["EmailORM"] = relationship(back_populates="tg_messages")


class Database:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path
        self.engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{sqlite_path}")
        self.Session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine, expire_on_commit=False
        )

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Soft migrations / data hygiene for SQLite.
            # 1) Ensure assets.type is non-null-ish for old rows.
            try:
                await conn.exec_driver_sql(
                    "UPDATE assets SET type = :t WHERE type IS NULL OR TRIM(type) = ''",
                    {"t": AssetType.UNCLASSIFIED.value},
                )
            except Exception:
                # table might not exist on first init; create_all will handle it
                pass
            # 2) Backfill assets from already ingested events (after upgrades / DB resets).
            try:
                await conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO assets (hostname, type) "
                    "SELECT DISTINCT device, :t FROM events "
                    "WHERE device IS NOT NULL AND TRIM(device) != ''",
                    {"t": AssetType.UNCLASSIFIED.value},
                )
            except Exception:
                pass

    # ===== Assets =====
    async def add_asset(self, hostname: str, asset_type: AssetType) -> None:
        hostname = hostname.strip()
        async with self.Session() as s:
            existing = await s.scalar(select(AssetORM).where(AssetORM.hostname == hostname))
            if existing:
                existing.type = asset_type.value
            else:
                s.add(AssetORM(hostname=hostname, type=asset_type.value))
            await s.commit()

    async def ensure_asset(self, hostname: str | None) -> AssetType | None:
        """
        Ensure hostname exists in DB.
        If missing -> create with UNCLASSIFIED.
        Returns current asset type (or None if hostname is empty).
        """
        if not hostname:
            return None
        hn = hostname.strip()
        if not hn:
            return None
        async with self.Session() as s:
            obj = await s.scalar(select(AssetORM).where(AssetORM.hostname == hn))
            if not obj:
                obj = AssetORM(hostname=hn, type=AssetType.UNCLASSIFIED.value)
                s.add(obj)
                await s.commit()
                return AssetType.UNCLASSIFIED
            try:
                return AssetType(obj.type)
            except Exception:
                obj.type = AssetType.UNCLASSIFIED.value
                await s.commit()
                return AssetType.UNCLASSIFIED

    async def get_asset_by_hostname(self, hostname: str | None) -> tuple[int, str, AssetType] | None:
        if not hostname:
            return None
        hn = hostname.strip()
        if not hn:
            return None
        async with self.Session() as s:
            obj = await s.scalar(select(AssetORM).where(AssetORM.hostname == hn))
            if not obj:
                return None
            try:
                t = AssetType(obj.type)
            except Exception:
                t = AssetType.UNCLASSIFIED
            return obj.id, obj.hostname, t

    # ===== Asset recipients (optional per-host notifications) =====
    async def upsert_asset_recipient(
        self,
        *,
        asset_id: int,
        chat_id: int,
        user_id: int,
        min_risk: str = "MEDIUM",
        enabled: bool = True,
    ) -> None:
        now = datetime.now(tz=UTC)
        mr = (min_risk or "MEDIUM").strip().upper()
        if mr not in ("INFO", "MEDIUM", "HIGH", "CRITICAL"):
            mr = "MEDIUM"
        async with self.Session() as s:
            existing = await s.scalar(
                select(AssetRecipientORM)
                .where(AssetRecipientORM.asset_id == asset_id)
                .where(AssetRecipientORM.chat_id == int(chat_id))
            )
            if existing:
                existing.user_id = int(user_id)
                existing.min_risk = mr
                existing.enabled = 1 if enabled else 0
            else:
                s.add(
                    AssetRecipientORM(
                        asset_id=asset_id,
                        chat_id=int(chat_id),
                        user_id=int(user_id),
                        enabled=1 if enabled else 0,
                        min_risk=mr,
                        created_at_utc=now,
                    )
                )
            await s.commit()

    async def list_asset_recipients(self, *, asset_id: int) -> list[tuple[int, int, int, str, bool]]:
        """
        Returns [(id, chat_id, user_id, min_risk, enabled)].
        """
        async with self.Session() as s:
            rows = (
                await s.execute(select(AssetRecipientORM).where(AssetRecipientORM.asset_id == asset_id))
            ).scalars().all()
        out: list[tuple[int, int, int, str, bool]] = []
        for r in rows:
            out.append((r.id, int(r.chat_id), int(r.user_id), (r.min_risk or "MEDIUM"), bool(r.enabled)))
        return out

    async def delete_asset_recipient(self, recipient_id: int) -> bool:
        async with self.Session() as s:
            obj = await s.get(AssetRecipientORM, recipient_id)
            if not obj:
                return False
            await s.delete(obj)
            await s.commit()
            return True

    async def clear_asset_recipients(self, *, asset_id: int) -> int:
        async with self.Session() as s:
            rows = (
                await s.execute(select(AssetRecipientORM).where(AssetRecipientORM.asset_id == asset_id))
            ).scalars().all()
            n = 0
            for r in rows:
                await s.delete(r)
                n += 1
            await s.commit()
            return n

    async def list_recipients_for_device(self, *, device: str | None) -> list[tuple[int, int, str, bool]]:
        """
        Returns [(chat_id, user_id, min_risk, enabled)] for a hostname/device.
        """
        if not device:
            return []
        d = device.strip()
        if not d:
            return []
        async with self.Session() as s:
            asset = await s.scalar(select(AssetORM).where(AssetORM.hostname == d))
            if not asset:
                return []
            rows = (
                await s.execute(select(AssetRecipientORM).where(AssetRecipientORM.asset_id == asset.id))
            ).scalars().all()
        out: list[tuple[int, int, str, bool]] = []
        for r in rows:
            out.append((int(r.chat_id), int(r.user_id), (r.min_risk or "MEDIUM"), bool(r.enabled)))
        return out

    async def remove_asset(self, hostname: str) -> bool:
        hostname = hostname.strip()
        async with self.Session() as s:
            obj = await s.scalar(select(AssetORM).where(AssetORM.hostname == hostname))
            if not obj:
                return False
            await s.delete(obj)
            await s.commit()
            return True

    async def delete_asset_by_id(self, asset_id: int) -> bool:
        async with self.Session() as s:
            obj = await s.get(AssetORM, asset_id)
            if not obj:
                return False
            await s.delete(obj)
            await s.commit()
            return True

    async def list_assets(self) -> list[tuple[str, AssetType]]:
        async with self.Session() as s:
            rows = (await s.execute(select(AssetORM).order_by(AssetORM.hostname.asc()))).scalars().all()
        out: list[tuple[str, AssetType]] = []
        for r in rows:
            try:
                out.append((r.hostname, AssetType(r.type)))
            except Exception:
                continue
        return out

    async def list_assets_detailed(self) -> list[tuple[int, str, AssetType]]:
        async with self.Session() as s:
            rows = (await s.execute(select(AssetORM))).scalars().all()
        out: list[tuple[int, str, AssetType]] = []
        for r in rows:
            try:
                t = AssetType(r.type)
            except Exception:
                t = AssetType.UNCLASSIFIED
            out.append((r.id, r.hostname, t))
        # sort: UNCLASSIFIED first, then hostname
        prio = {AssetType.UNCLASSIFIED: 0, AssetType.SERVER: 1, AssetType.WORKSTATION: 2}
        out.sort(key=lambda x: (prio.get(x[2], 9), (x[1] or "").lower()))
        return out

    async def get_asset_by_id(self, asset_id: int) -> tuple[int, str, AssetType] | None:
        async with self.Session() as s:
            obj = await s.get(AssetORM, asset_id)
            if not obj:
                return None
            try:
                t = AssetType(obj.type)
            except Exception:
                t = AssetType.UNCLASSIFIED
            return obj.id, obj.hostname, t

    async def set_asset_type_by_id(self, asset_id: int, asset_type: AssetType) -> bool:
        async with self.Session() as s:
            obj = await s.get(AssetORM, asset_id)
            if not obj:
                return False
            obj.type = asset_type.value
            await s.commit()
            return True

    async def get_asset_type(self, hostname: str | None) -> AssetType | None:
        if not hostname:
            return None
        hostname = hostname.strip()
        if not hostname:
            return None
        async with self.Session() as s:
            obj = await s.scalar(select(AssetORM).where(AssetORM.hostname == hostname))
            if not obj:
                return None
            try:
                return AssetType(obj.type)
            except Exception:
                return None

    async def list_servers(self) -> list[str]:
        async with self.Session() as s:
            rows = (
                await s.execute(select(AssetORM).where(AssetORM.type == AssetType.SERVER.value))
            ).scalars().all()
        return [r.hostname for r in rows]

    # ===== Ingest =====
    async def upsert_email(
        self,
        uid: str,
        raw_text: str,
        message_id: str | None = None,
        subject: str | None = None,
        from_email: str | None = None,
        date_utc: datetime | None = None,
    ) -> tuple[int, bool]:
        async with self.Session() as s:
            existing = await s.scalar(select(EmailORM).where(EmailORM.uid == uid))
            if existing:
                # не перезаписываем raw_text чтобы callback "Подробности" был стабилен
                return existing.id, False
            obj = EmailORM(
                uid=uid,
                raw_text=raw_text,
                message_id=message_id,
                subject=subject,
                from_email=from_email,
                date_utc=date_utc,
            )
            s.add(obj)
            await s.commit()
            await s.refresh(obj)
            return obj.id, True

    async def mark_email_telegram_sent(self, email_id: int, when_utc: datetime) -> None:
        async with self.Session() as s:
            obj = await s.get(EmailORM, email_id)
            if not obj:
                return
            obj.telegram_sent = 1
            obj.telegram_sent_at_utc = when_utc
            await s.commit()

    async def add_telegram_message(
        self,
        *,
        email_id: int,
        device: str | None,
        chat_id: int,
        telegram_message_id: int,
        sent_at_utc: datetime,
        risk_level: str,
        ai_enabled: bool,
        model_used: str | None,
        llm_fallback: str | None,
        text_sent: str,
    ) -> int:
        async with self.Session() as s:
            obj = TelegramMessageORM(
                email_id=email_id,
                device=(device.strip() if device else None),
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                sent_at_utc=sent_at_utc,
                risk_level=risk_level,
                ai_enabled=1 if ai_enabled else 0,
                model_used=model_used,
                llm_fallback=llm_fallback,
                text_sent=text_sent,
            )
            s.add(obj)
            await s.commit()
            await s.refresh(obj)
            return obj.id

    async def list_telegram_history_for_device(
        self,
        *,
        device: str,
        since_utc: datetime,
        limit: int,
        offset: int = 0,
    ) -> list[tuple[int, datetime, str, int, str | None]]:
        """
        Returns list of sent alerts for a device:
        (tg_msg_pk, sent_at_utc, risk_level, email_id, model_used)
        """
        d = device.strip()
        if not d:
            return []
        async with self.Session() as s:
            q = (
                select(
                    TelegramMessageORM.id,
                    TelegramMessageORM.sent_at_utc,
                    TelegramMessageORM.risk_level,
                    TelegramMessageORM.email_id,
                    TelegramMessageORM.model_used,
                )
                .where(TelegramMessageORM.device == d)
                .where(TelegramMessageORM.sent_at_utc >= since_utc)
                .order_by(TelegramMessageORM.sent_at_utc.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = (await s.execute(q)).all()
        return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    async def count_telegram_history_for_device(self, *, device: str, since_utc: datetime) -> int:
        d = device.strip()
        if not d:
            return 0
        async with self.Session() as s:
            q = select(TelegramMessageORM.id).where(TelegramMessageORM.device == d).where(
                TelegramMessageORM.sent_at_utc >= since_utc
            )
            rows = (await s.execute(q)).all()
        return len(rows)

    async def get_telegram_message_text(self, tg_msg_id: int) -> tuple[str, int] | None:
        """
        Returns (text_sent, email_id) for a stored Telegram message.
        """
        async with self.Session() as s:
            obj = await s.get(TelegramMessageORM, tg_msg_id)
            if not obj:
                return None
            return obj.text_sent, obj.email_id

    async def is_email_telegram_sent(self, email_id: int) -> bool:
        async with self.Session() as s:
            obj = await s.get(EmailORM, email_id)
            return bool(obj and obj.telegram_sent)

    async def get_latest_telegram_message_for_email(self, email_id: int) -> tuple[str, str] | None:
        """
        Returns (text_sent, risk_level) for the latest stored Telegram message for this email_id.
        Useful for "replay" to newly bound recipients without re-running AI.
        """
        async with self.Session() as s:
            q = (
                select(TelegramMessageORM)
                .where(TelegramMessageORM.email_id == email_id)
                .order_by(TelegramMessageORM.id.desc())
                .limit(1)
            )
            obj = (await s.execute(q)).scalars().first()
            if not obj:
                return None
            return obj.text_sent, obj.risk_level

    async def has_telegram_message(self, *, email_id: int, chat_id: int) -> bool:
        async with self.Session() as s:
            q = (
                select(TelegramMessageORM.id)
                .where(TelegramMessageORM.email_id == email_id)
                .where(TelegramMessageORM.chat_id == int(chat_id))
                .limit(1)
            )
            return (await s.execute(q)).first() is not None

    async def insert_event(self, email_id: int, ev: KasperskyEvent) -> int:
        now = datetime.now(tz=UTC)
        fp = ev.fingerprint()
        async with self.Session() as s:
            obj = EventORM(
                email_id=email_id,
                vendor_severity=ev.vendor_severity,
                device=ev.device,
                event_type=ev.event_type,
                detection_name=ev.detection_name,
                object_path=ev.object_path,
                process_name=ev.process_name,
                sha256=ev.sha256,
                user=ev.user,
                result=ev.result,
                event_time_utc=ev.event_time,
                fingerprint=fp,
                created_at_utc=now,
            )
            s.add(obj)
            await s.commit()
            await s.refresh(obj)
            return obj.id

    async def recent_devices_for_detection(
        self, *, detection_name: str | None, since_utc: datetime, limit: int = 50
    ) -> list[str]:
        """
        Returns distinct device names for recent events matching the same detection_name.
        Helps hint 'lateral movement' patterns.
        """
        if not detection_name:
            return []
        async with self.Session() as s:
            q = (
                select(EventORM.device)
                .where(EventORM.created_at_utc >= since_utc)
                .where(EventORM.detection_name == detection_name)
                .where(EventORM.device.is_not(None))
                .limit(limit)
            )
            rows = (await s.execute(q)).scalars().all()
        # distinct, stable order
        seen: set[str] = set()
        out: list[str] = []
        for d in rows:
            if not d:
                continue
            if d in seen:
                continue
            seen.add(d)
            out.append(d)
        return out

    async def get_email_raw_text(self, email_id: int) -> str | None:
        async with self.Session() as s:
            obj = await s.get(EmailORM, email_id)
            return obj.raw_text if obj else None

    # ===== Dedup =====
    async def should_send_alert(
        self,
        fingerprint: str,
        now_utc: datetime,
        window_seconds: int,
        repeat_threshold: int,
        email_id: int,
    ) -> tuple[bool, int]:
        """
        Returns (send_alert, current_count).

        Spec:
        - Если повтор в окне window_seconds: инкрементировать счетчик, не слать новый алерт
          до превышения repeat_threshold.
        """
        async with self.Session() as s:
            row = await s.get(DedupORM, fingerprint)
            if row is None:
                s.add(
                    DedupORM(
                        fingerprint=fingerprint,
                        first_seen_utc=now_utc,
                        last_seen_utc=now_utc,
                        count=1,
                        last_alert_at_utc=now_utc,
                        last_email_id=email_id,
                    )
                )
                await s.commit()
                return True, 1

            def _as_utc(dt: datetime | None) -> datetime | None:
                if dt is None:
                    return None
                # SQLite often returns naive datetimes even if timezone=True
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)

            row.last_seen_utc = _as_utc(row.last_seen_utc) or now_utc
            row.last_alert_at_utc = _as_utc(row.last_alert_at_utc)

            within = (now_utc - row.last_seen_utc) <= timedelta(seconds=window_seconds)
            row.last_seen_utc = now_utc
            row.count += 1
            row.last_email_id = email_id

            send = False
            if not within:
                # окно истекло — считаем это новым алертом
                row.last_alert_at_utc = now_utc
                send = True
            elif row.count >= repeat_threshold and (
                row.last_alert_at_utc is None
                or (now_utc - row.last_alert_at_utc) > timedelta(seconds=window_seconds)
            ):
                # превышен порог в окне — шлем "бёрст" сообщение, но не чаще окна
                row.last_alert_at_utc = now_utc
                send = True

            await s.commit()
            return send, row.count

