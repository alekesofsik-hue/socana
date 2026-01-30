from __future__ import annotations

import asyncio
import imaplib
from dataclasses import dataclass


@dataclass(frozen=True)
class ImapMessage:
    uid: str
    raw: bytes


class ImapClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        mailbox: str = "INBOX",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.mailbox = mailbox

    async def fetch_unseen_from(self, from_email: str, limit: int = 20) -> list[ImapMessage]:
        return await asyncio.to_thread(self._fetch_from_sync, from_email, limit, True)

    async def fetch_latest_from(self, from_email: str, limit: int = 20) -> list[ImapMessage]:
        """Fetch last N messages FROM sender regardless of \\Seen flag (useful for tests)."""
        return await asyncio.to_thread(self._fetch_from_sync, from_email, limit, False)

    async def mark_seen_many(self, uids: list[str]) -> int:
        """Mark given UIDs as \\Seen in the selected mailbox. Returns how many were attempted."""
        if not uids:
            return 0
        return await asyncio.to_thread(self._mark_seen_many_sync, uids)

    async def debug_mailbox(self, from_email: str, sample: int = 10) -> dict:
        """
        Debug helper for test runs:
        - lists mailboxes
        - counts ALL/UNSEEN and UNSEEN FROM-filter
        - fetches headers+flags for a few latest messages
        """
        return await asyncio.to_thread(self._debug_mailbox_sync, from_email, sample)

    def _fetch_from_sync(self, from_email: str, limit: int, unseen_only: bool) -> list[ImapMessage]:
        imap = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            imap.login(self.username, self.password)
            imap.select(self.mailbox)

            criteria = f'(FROM "{from_email}")'
            if unseen_only:
                criteria = f'(UNSEEN FROM "{from_email}")'

            typ, data = imap.uid("SEARCH", None, criteria)
            if typ != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()
            # берем последние limit
            uids = uids[-limit:]

            out: list[ImapMessage] = []
            for uid_b in uids:
                uid = uid_b.decode("utf-8", errors="ignore")
                # BODY.PEEK[] does NOT set \\Seen flag (important for UNSEEN-based polling)
                typ2, msg_data = imap.uid("FETCH", uid_b, "(BODY.PEEK[])")
                if typ2 != "OK" or not msg_data:
                    continue
                # msg_data: [(b'UID ... RFC822 {bytes}', raw_bytes), b')']
                raw = None
                for item in msg_data:
                    if isinstance(item, tuple) and len(item) == 2:
                        raw = item[1]
                        break
                if raw:
                    out.append(ImapMessage(uid=uid, raw=raw))
            return out
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _mark_seen_many_sync(self, uids: list[str]) -> int:
        imap = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            imap.login(self.username, self.password)
            imap.select(self.mailbox)
            for uid in uids:
                try:
                    imap.uid("STORE", uid, "+FLAGS.SILENT", "(\\Seen)")
                except Exception:
                    # best-effort; do not break ack batch
                    continue
            return len(uids)
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _debug_mailbox_sync(self, from_email: str, sample: int) -> dict:
        imap = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            imap.login(self.username, self.password)

            typ_list, boxes = imap.list()
            mailbox_list = []
            if typ_list == "OK" and boxes:
                for b in boxes:
                    try:
                        mailbox_list.append(b.decode("utf-8", errors="ignore"))
                    except Exception:
                        mailbox_list.append(str(b))

            typ_sel, sel = imap.select(self.mailbox)
            selected_ok = typ_sel == "OK"
            selected_count = sel[0].decode("utf-8", errors="ignore") if (sel and sel[0]) else None

            def _uid_search(criteria: str) -> list[str]:
                t, d = imap.uid("SEARCH", None, criteria)
                if t != "OK" or not d or not d[0]:
                    return []
                out = []
                for x in d[0].split():
                    try:
                        out.append(x.decode("utf-8", errors="ignore"))
                    except Exception:
                        out.append(str(x))
                return out

            all_uids = _uid_search("ALL") if selected_ok else []
            unseen_uids = _uid_search("UNSEEN") if selected_ok else []
            unseen_from_uids = (
                _uid_search(f'(UNSEEN FROM "{from_email}")') if selected_ok else []
            )
            from_all_uids = _uid_search(f'(FROM "{from_email}")') if selected_ok else []

            # Fetch a few latest headers/flags to see real From and flags
            latest = all_uids[-sample:] if sample and all_uids else []
            samples = []
            for uid in latest:
                t, d = imap.uid("FETCH", uid.encode("utf-8"), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if t != "OK" or not d:
                    continue
                flags = None
                header = ""
                for item in d:
                    if isinstance(item, tuple) and len(item) == 2:
                        header = item[1].decode("utf-8", errors="replace")
                        # flags are in item[0] bytes, try to show them
                        try:
                            flags = item[0].decode("utf-8", errors="ignore")
                        except Exception:
                            flags = str(item[0])
                        break
                samples.append({"uid": uid, "flags_line": flags, "header": header.strip()})

            return {
                "host": self.host,
                "port": self.port,
                "mailbox": self.mailbox,
                "mailboxes": mailbox_list,
                "selected_ok": selected_ok,
                "selected_count": selected_count,
                "counts": {
                    "all": len(all_uids),
                    "unseen": len(unseen_uids),
                    "unseen_from": len(unseen_from_uids),
                    "from_all": len(from_all_uids),
                },
                "samples": samples,
            }
        finally:
            try:
                imap.logout()
            except Exception:
                pass
