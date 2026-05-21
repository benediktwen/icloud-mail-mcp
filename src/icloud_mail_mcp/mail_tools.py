"""MCP tool registration for iCloud Mail."""

from mcp.server.fastmcp import FastMCP

from .imap_client import IMAPClient

_client = IMAPClient()


def register_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def search_emails(
        query: str = "",
        folder: str = "INBOX",
        unread_only: bool = False,
        since: str = "",
        before: str = "",
        max_results: int = 20,
    ) -> list[dict]:
        """Search emails. Returns [{id, subject, from, date, snippet}].

        Args:
            query: Free-text search string (searches headers and body).
            folder: IMAP folder to search (default: INBOX).
            unread_only: If true, return only unread messages.
            since: Earliest date in DD-Mon-YYYY format, e.g. "01-Jan-2025".
            before: Latest date in DD-Mon-YYYY format.
            max_results: Maximum number of results (default 20, max 50).
        """
        return _client.search(
            query=query,
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
    def label_email(uid: str, destination_folder: str, source_folder: str = "INBOX") -> dict:
        """Move an email to a different folder.

        Args:
            uid: Message UID from search_emails.
            destination_folder: Target folder name (from list_labels).
            source_folder: Current folder (default: INBOX).
        """
        _client.move_to_folder(uid=uid, destination=destination_folder)
        return {"ok": True, "uid": uid, "moved_to": destination_folder}

    @mcp.tool()
    def mark_read(uid: str) -> dict:
        """Mark a message as read.

        Args:
            uid: Message UID from search_emails.
        """
        _client.mark_read(uid=uid)
        return {"ok": True, "uid": uid}
