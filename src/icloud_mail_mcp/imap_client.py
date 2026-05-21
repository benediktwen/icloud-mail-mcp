"""iCloud IMAP client with reconnect logic.

iCloud drops idle IMAP connections after ~30 minutes. All operations go through
_with_reconnect(), which catches imaplib.abort and re-establishes the connection.
"""

import email
import imaplib
import os
import re
import time
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


class IMAPClient:
    def __init__(self) -> None:
        self._conn: imaplib.IMAP4_SSL | None = None
        self._selected_folder: str | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(
            os.environ["ICLOUD_EMAIL"],
            os.environ["ICLOUD_APP_PASSWORD"],
        )
        return conn

    def _ensure_connected(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self._conn = self._connect()
            self._selected_folder = None
        return self._conn

    def _reconnect(self) -> imaplib.IMAP4_SSL:
        try:
            if self._conn:
                self._conn.logout()
        except Exception:
            pass
        self._conn = None
        self._selected_folder = None
        self._conn = self._connect()
        return self._conn

    def _with_reconnect(self, fn, *args, **kwargs):
        for attempt in range(2):
            try:
                conn = self._ensure_connected()
                return fn(conn, *args, **kwargs)
            except imaplib.IMAP4.abort:
                print("IMAP connection aborted by server, reconnecting...")
                self._conn = None
                self._selected_folder = None
                if attempt == 1:
                    raise

    def _select(self, conn: imaplib.IMAP4_SSL, folder: str) -> None:
        if self._selected_folder != folder:
            typ, _ = conn.select(f'"{folder}"')
            if typ != "OK":
                raise ValueError(f"Cannot select folder {folder!r}")
            self._selected_folder = folder

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def list_folders(self) -> list[str]:
        def _run(conn):
            typ, data = conn.list()
            if typ != "OK":
                return []
            folders = []
            for item in data:
                if not item:
                    continue
                decoded = item.decode("utf-8") if isinstance(item, bytes) else item
                # Parse: (\HasNoChildren) "/" "INBOX"
                m = re.search(r'"([^"]+)"$', decoded)
                if m:
                    folders.append(m.group(1))
                else:
                    parts = decoded.rsplit(" ", 1)
                    if len(parts) == 2:
                        folders.append(parts[-1].strip('"'))
            return sorted(folders)

        return self._with_reconnect(_run)

    def move_to_folder(self, uid: str, destination: str) -> None:
        def _run(conn):
            self._select(conn, "INBOX")
            conn.uid("COPY", uid, f'"{destination}"')
            conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            conn.expunge()

        self._with_reconnect(_run)

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def mark_read(self, uid: str) -> None:
        def _run(conn):
            # UID might be in any folder — search INBOX first, then All Mail
            for folder in ["INBOX", "All Mail", "[Gmail]/All Mail"]:
                try:
                    self._select(conn, folder)
                    typ, data = conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
                    if typ == "OK":
                        return
                except Exception:
                    continue
            raise ValueError(f"Message UID {uid!r} not found in accessible folders")

        self._with_reconnect(_run)

    def search(
        self,
        query: str = "",
        folder: str = "INBOX",
        unread_only: bool = False,
        since: str | None = None,
        before: str | None = None,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Search messages. Returns list of {id, subject, from, date, snippet}."""

        def _run(conn):
            self._select(conn, folder)

            criteria: list[str] = []
            if unread_only:
                criteria.append("UNSEEN")
            if since:
                criteria.append(f'SINCE "{since}"')
            if before:
                criteria.append(f'BEFORE "{before}"')
            if query:
                criteria.append(f'TEXT "{query}"')
            if not criteria:
                criteria.append("ALL")

            search_str = " ".join(criteria)
            typ, data = conn.uid("SEARCH", None, search_str)
            if typ != "OK" or not data or not data[0]:
                return []

            uids = data[0].split()
            uids = uids[-max_results:]  # most recent last

            if not uids:
                return []

            uid_list = b",".join(uids)
            typ, msg_data = conn.uid(
                "FETCH", uid_list, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            if typ != "OK":
                return []

            results = []
            for i in range(0, len(msg_data), 2):
                part = msg_data[i]
                if not isinstance(part, tuple):
                    continue
                uid_str = _extract_uid(msg_data[i][0])
                raw = part[1]
                msg = email.message_from_bytes(raw)
                snippet = _fetch_snippet(conn, uid_str)
                results.append(
                    {
                        "id": uid_str,
                        "subject": _decode_header(msg.get("Subject", "")),
                        "from": _decode_header(msg.get("From", "")),
                        "date": msg.get("Date", ""),
                        "snippet": snippet,
                    }
                )

            results.reverse()  # newest first
            return results

        return self._with_reconnect(_run)

    def get_email(self, uid: str, folder: str = "INBOX") -> dict[str, Any]:
        """Fetch a full message by UID."""

        def _run(conn):
            self._select(conn, folder)
            typ, msg_data = conn.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                raise ValueError(f"Message {uid!r} not found in {folder!r}")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            return {
                "id": uid,
                "subject": _decode_header(msg.get("Subject", "")),
                "from": _decode_header(msg.get("From", "")),
                "to": _decode_header(msg.get("To", "")),
                "date": msg.get("Date", ""),
                "body": _extract_body(msg),
            }

        return self._with_reconnect(_run)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_uid(fetch_response_line: bytes) -> str:
    m = re.search(rb"UID (\d+)", fetch_response_line)
    return m.group(1).decode() if m else ""


def _fetch_snippet(conn: imaplib.IMAP4_SSL, uid: str, length: int = 200) -> str:
    try:
        typ, data = conn.uid("FETCH", uid, f"(BODY.PEEK[TEXT]<0.{length}>)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        raw = data[0][1]
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        return text[:length].replace("\r\n", " ").replace("\n", " ").strip()
    except Exception:
        return ""


def _extract_body(msg: email.message.Message) -> str:
    """Return plain text body, falling back to HTML stripped of tags."""
    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                payload = part.get_payload(decode=True)
                if payload:
                    plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and not html:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text

    if plain:
        return plain
    if html:
        return re.sub(r"<[^>]+>", "", html)
    return ""
