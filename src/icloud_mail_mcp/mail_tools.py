"""MCP tool registration for iCloud Mail."""

from mcp.server.fastmcp import FastMCP

from .imap_client import IMAPClient

_client = IMAPClient()


def register_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def search_emails(
        query: str = "",
        from_address: str = "",
        folder: str = "INBOX",
        unread_only: bool = False,
        since: str = "",
        before: str = "",
        max_results: int = 20,
    ) -> list[dict]:
        """Search emails. Returns [{id, subject, from, date, snippet}].

        Args:
            query: Free-text search string (searches headers and body);
                   umlauts and other non-ASCII characters are fine.
            from_address: Filter by exact sender address using IMAP FROM criterion
                          (more reliable than query for sender searches).
            folder: IMAP folder to search (default: INBOX).
            unread_only: If true, return only unread messages.
            since: Earliest date in DD-Mon-YYYY format, e.g. "01-Jan-2025".
            before: Latest date in DD-Mon-YYYY format.
            max_results: Maximum number of results (default 20, max 50).
        """
        return _client.search(
            query=query,
            from_address=from_address,
            folder=folder,
            unread_only=unread_only,
            since=since or None,
            before=before or None,
            max_results=min(max_results, 50),
        )

    @mcp.tool()
    def get_email(uid: str, folder: str = "INBOX") -> dict:
        """Fetch the full body of a message by its UID.

        Args:
            uid: Message UID from search_emails.
            folder: IMAP folder the message lives in (default: INBOX).
        """
        return _client.get_email(uid=uid, folder=folder)

    @mcp.tool()
    def list_labels() -> list[str]:
        """List all IMAP folders / labels available in the mailbox."""
        return _client.list_folders()

    @mcp.tool()
    def move_email(uid: str, destination_folder: str, source_folder: str = "INBOX") -> dict:
        """Move an email to a different folder.

        Args:
            uid: Message UID from search_emails.
            destination_folder: Target folder name (from list_labels).
            source_folder: Current folder (default: INBOX).
        """
        _client.move_to_folder(uid=uid, destination=destination_folder, source_folder=source_folder)
        return {"ok": True, "uid": uid, "moved_to": destination_folder}

    @mcp.tool()
    def delete_email(uid: str, folder: str = "INBOX") -> dict:
        """Move an email to Trash (Deleted Messages).

        Args:
            uid: Message UID from search_emails.
            folder: Current folder the message lives in (default: INBOX).
        """
        _client.delete_email(uid=uid, folder=folder)
        return {"ok": True, "uid": uid}

    @mcp.tool()
    def mark_read(uid: str, folder: str = "INBOX") -> dict:
        """Mark a message as read.

        Args:
            uid: Message UID from search_emails.
            folder: Folder the message lives in (default: INBOX).
        """
        _client.mark_read(uid=uid, folder=folder)
        return {"ok": True, "uid": uid}

    @mcp.tool()
    def mark_unread(uid: str, folder: str = "INBOX") -> dict:
        """Mark a message as unread.

        Args:
            uid: Message UID from search_emails.
            folder: Folder the message lives in (default: INBOX).
        """
        _client.mark_unread(uid=uid, folder=folder)
        return {"ok": True, "uid": uid}

    @mcp.tool()
    def flag_email(uid: str, folder: str = "INBOX") -> dict:
        """Flag (star) a message.

        Args:
            uid: Message UID from search_emails.
            folder: Folder the message lives in (default: INBOX).
        """
        _client.flag_email(uid=uid, folder=folder)
        return {"ok": True, "uid": uid}

    @mcp.tool()
    def unflag_email(uid: str, folder: str = "INBOX") -> dict:
        """Remove the flag (star) from a message.

        Args:
            uid: Message UID from search_emails.
            folder: Folder the message lives in (default: INBOX).
        """
        _client.unflag_email(uid=uid, folder=folder)
        return {"ok": True, "uid": uid}

    @mcp.tool()
    def create_draft(
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        attachments: list[dict] | None = None,
    ) -> dict:
        """Create a new draft email in the Drafts folder.

        The draft appears in Apple Mail (and any IMAP client) ready to send.
        No email is sent; you review and send manually.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional CC address(es), comma-separated.
            attachments: Optional list of file attachments. Each item must have:
                - filename (str): Name shown in the email, e.g. "report.pdf".
                - data_base64 (str): File contents encoded as base64.
                - content_type (str, optional): MIME type, e.g. "application/pdf".
                  Defaults to "application/octet-stream".
                To attach a file: read it, base64-encode its bytes, pass here.
        """
        return _client.create_draft(to=to, subject=subject, body=body, cc=cc, attachments=attachments)

    @mcp.tool()
    def create_draft_reply(
        uid: str,
        reply_text: str = "",
        folder: str = "INBOX",
        attachments: list[dict] | None = None,
        reply_html: str = "",
        latest_in_thread: bool = True,
    ) -> dict:
        """Create a draft reply to an existing email, laid out like Apple Mail.

        The draft is saved to the Drafts folder and looks in Apple Mail like a
        reply made there: HTML + plain text, "On <date>, at <time>, <name>
        <<address>> wrote:" and the original (with its formatting and inline
        images) quoted below. Subject gets exactly one "Re:"; In-Reply-To and
        References keep the thread. Replying to an own mail (e.g. folder
        "Sent Messages") addresses its original To/Cc, never yourself.
        No email is sent; you review and send manually.

        By default the reply goes to the NEWEST message of the thread (searched
        across all mail folders, "Sent Messages" included), whichever message
        uid points to — like pressing Reply on the last message in Apple Mail.
        If that last message is your own, the draft goes to its recipients.
        The result says which message was answered (replied_to,
        switched_to_latest).

        Args:
            uid: UID of a message of the thread (from search_emails).
            reply_text: Your reply as plain text, placed above the quoted original.
                Blank lines separate paragraphs.
            folder: Folder the original message lives in (default: INBOX).
            attachments: Optional list of file attachments. Each item must have:
                - filename (str): Name shown in the email, e.g. "report.pdf".
                - data_base64 (str): File contents encoded as base64.
                - content_type (str, optional): MIME type, e.g. "application/pdf".
                  Defaults to "application/octet-stream".
            reply_html: Optional HTML for your reply (e.g. with <ul> lists or
                <b>). Replaces the HTML generated from reply_text; the plain-text
                part still uses reply_text (or this HTML as text if reply_text
                is empty).
            latest_in_thread: Default true. Set false to answer exactly the
                message given by uid/folder, even if newer ones exist.

        Returns {ok, uid, folder, draft_to, draft_cc, draft_from, subject,
        inline_images, replied_to, switched_to_latest, attachment_count};
        uid is the draft's UID in Drafts.
        """
        return _client.create_draft_reply(
            uid=uid,
            folder=folder,
            reply_text=reply_text,
            attachments=attachments,
            reply_html=reply_html,
            latest_in_thread=latest_in_thread,
        )

    @mcp.tool()
    def read_pdf_attachment(uid: str, filename: str, folder: str = "INBOX", max_pages: int = 50) -> dict:
        """Extract and return the plain text content of a PDF email attachment.

        Use get_email first to see the list of attachments and their filenames.
        Returns {page_count, pages_read, truncated, text}. Non-PDF attachments
        are listed in get_email but cannot be read with this tool.

        Args:
            uid: Message UID from search_emails.
            filename: Exact filename of the PDF attachment (from get_email attachments list).
            folder: Folder the message lives in (default: INBOX).
            max_pages: Maximum number of pages to extract (default 50).
        """
        return _client.get_pdf_text(uid=uid, filename=filename, folder=folder, max_pages=max_pages)
