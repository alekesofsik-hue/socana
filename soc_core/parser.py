from __future__ import annotations

import re
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from soc_core.models import KasperskyEvent, ParsedEmail


_KV_RE = re.compile(r"^\s*([^:\n]{2,80})\s*:\s*(.*?)\s*$")
_DEVICE_RE = re.compile(r"произошло на устройстве\s+([A-Za-z0-9_.-]+)", re.IGNORECASE)
_VENDOR_SEV_RE = re.compile(r"произошло\s+(\w+)\s+событие", re.IGNORECASE)
_EVENT_QUOTED_RE = re.compile(r'событие\s+"([^"]+)"', re.IGNORECASE)


def _extract_best_body(msg) -> tuple[str, str]:
    """
    Returns (content_type, text) where content_type is "text/plain" or "text/html".
    Prefers HTML because Kaspersky письма часто табличные/HTML.
    """
    if msg.is_multipart():
        parts = list(msg.walk())
        html_part = None
        text_part = None
        for p in parts:
            ctype = p.get_content_type()
            if ctype == "text/html" and html_part is None:
                html_part = p
            if ctype == "text/plain" and text_part is None:
                text_part = p
        chosen = html_part or text_part
        if chosen is None:
            return ("text/plain", "")
        payload = chosen.get_payload(decode=True) or b""
        charset = chosen.get_content_charset() or "utf-8"
        return (chosen.get_content_type(), payload.decode(charset, errors="replace"))
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return (msg.get_content_type(), payload.decode(charset, errors="replace"))


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    # нормализуем пустые строки
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _guess_date(header_date: str | None) -> datetime | None:
    if not header_date:
        return None
    try:
        dt = parsedate_to_datetime(header_date)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _parse_key_values(text: str) -> dict[str, str]:
    # Backward-compat helper (keeps first occurrence).
    kv: dict[str, str] = {}
    for k, v in _parse_kv_stream(text):
        if k not in kv:
            kv[k] = v
    return kv


def _parse_kv_stream(text: str) -> list[tuple[str, str]]:
    """
    Preserves order and duplicates (важно: "Название:" бывает и для процесса, и для угрозы).
    """
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _KV_RE.match(line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if key and val:
            out.append((key, val))
    return out


def _pick(kv: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        kk = k.strip().lower()
        if kk in kv and kv[kk]:
            return kv[kk]
    return None


def _pick_first(stream: list[tuple[str, str]], *keys: str) -> str | None:
    keyset = {k.strip().lower() for k in keys}
    for k, v in stream:
        if k in keyset and v:
            return v
    return None


def _all(stream: list[tuple[str, str]], *keys: str) -> list[str]:
    keyset = {k.strip().lower() for k in keys}
    return [v for k, v in stream if k in keyset and v]


def _select_process_and_detection(names: list[str]) -> tuple[str | None, str | None]:
    """
    Heuristics for Kaspersky Russian emails:
    - process_name обычно заканчивается на .exe/.dll
    - detection_name часто содержит ":" и начинается с HEUR/Trojan/not-a-virus/etc
    """
    process = None
    detection = None

    for n in names:
        nn = n.strip()
        low = nn.lower()
        if process is None and re.search(r"\.(exe|dll|sys|bat|ps1)\b", low):
            process = nn

    for n in names:
        nn = n.strip()
        low = nn.lower()
        if detection is not None:
            continue
        if any(low.startswith(p) for p in ("heur:", "trojan.", "trojan:", "not-a-virus:", "virus.", "worm.", "exploit.")):
            detection = nn
            continue
        if ":" in nn and not re.search(r"\.(exe|dll|sys)\b", low):
            detection = nn

    # fallback: если нет процесса — единственное имя считать детектом
    if detection is None and process is None and names:
        detection = names[-1].strip()

    # если нашли только exe и ничего похожего на сигнатуру — детектом считать последний non-exe (если есть)
    if detection is None and names:
        for n in reversed(names):
            low = n.lower()
            if not re.search(r"\.(exe|dll|sys)\b", low):
                detection = n.strip()
                break

    return process, detection


class KasperskyEmailParser:
    """
    Детерминированный слой: email bytes -> ParsedEmail -> KasperskyEvent
    """

    def parse(self, uid: str, raw_email: bytes) -> ParsedEmail:
        msg = BytesParser(policy=policy.default).parsebytes(raw_email)

        subject = str(msg.get("Subject", "")).strip() or None
        from_email = str(msg.get("From", "")).strip() or None
        message_id = str(msg.get("Message-Id", "")).strip() or None
        header_date = str(msg.get("Date", "")).strip() or None
        date = _guess_date(header_date)

        ctype, body = _extract_best_body(msg)
        raw_text = _html_to_text(body) if ctype == "text/html" else body.strip()

        stream = _parse_kv_stream(raw_text)
        kv = _parse_key_values(raw_text)  # legacy dict (first occurrences)

        # vendor severity обычно в subject/body: "Произошло Warning/Critical событие ..."
        vendor_severity = None
        for src in (subject or "", raw_text):
            msev = _VENDOR_SEV_RE.search(src)
            if msev:
                vendor_severity = msev.group(1).strip()
                break

        device = _pick(kv, "device", "computer", "host", "hostname", "устройство")
        if not device:
            mdev = _DEVICE_RE.search(raw_text)
            if mdev:
                device = mdev.group(1).strip()

        # "Название:" встречается дважды: процесс и имя угрозы
        names = _all(stream, "название", "name")
        process_name, detection_name = _select_process_and_detection(names)

        event = KasperskyEvent(
            vendor_severity=vendor_severity or _pick(kv, "severity", "vendor severity", "уровень опасности"),
            device=device,
            event_type=_pick_first(stream, "тип события", "event type", "event", "тип")
            or _pick(kv, "event type", "тип события"),
            detection_name=detection_name or _pick(kv, "detection name", "threat name", "malware name", "название угрозы"),
            object_path=_pick_first(stream, "объект", "object", "object path", "file", "path"),
            process_name=process_name or _pick(kv, "process", "process name", "процесс"),
            sha256=_pick(kv, "sha256", "sha-256", "hash", "хеш"),
            user=_pick_first(stream, "пользователь", "user", "account"),
            result=_pick_first(stream, "описание результата", "result", "action", "status", "результат"),
            event_time=_pick_first(stream, "дата и время события", "event time", "time", "date", "дата/время", "время события")
            or date,
        )

        # fallback: Тип события может быть только в строке "Событие \"...\" произошло ..."
        if not event.event_type:
            m = _EVENT_QUOTED_RE.search(raw_text)
            if m:
                event.event_type = m.group(1).strip()

        # если внутри письма ключей мало — попробуем fallback regexp по всему тексту
        if event.sha256 is None:
            m = re.search(r"\b[a-fA-F0-9]{64}\b", raw_text)
            if m:
                event.sha256 = m.group(0).lower()

        return ParsedEmail(
            uid=uid,
            message_id=message_id,
            subject=subject,
            from_email=from_email,
            date=date,
            raw_text=raw_text,
            event=event,
        )

