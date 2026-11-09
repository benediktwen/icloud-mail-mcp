"""Build reply drafts that look like a reply created in Apple Mail.

Pure functions only (no IMAP), so the whole draft can be unit-tested.

The draft is a ``multipart/alternative`` with a ``text/plain`` part and a
``text/html`` part. The HTML part follows Apple Mail's own reply layout:

    <reply>
    <div><br><blockquote type="cite">
      <div>On 05.11.2026, at 09:26, Name &lt;addr&gt; wrote:</div>
      <br class="Apple-interchange-newline">
      <div>original HTML body</div>
    </blockquote></div>

Inline images of the original (``cid:``) are carried over in a
``multipart/related`` part next to the HTML; ``cid:`` references without a
matching image part are removed so no broken placeholders appear.
"""

from __future__ import annotations

import copy
import email
import html
import os
import re
from email.header import Header, decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parsedate_to_datetime
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Europe/Berlin")

# Reply prefixes collapsed into a single "Re:" (English, German, Nordic, French).
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|aw|antw|antwort|sv|vs)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*)+", re.I)

_HTML_BODY_STYLE = "overflow-wrap: break-word; -webkit-nbsp-mode: space; line-break: after-white-space;"


# ----------------------------------------------------------------------
# Headers
# ----------------------------------------------------------------------

def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _unfold(value: str | None) -> str:
    return " ".join((value or "").split())


def own_addresses(extra: list[str] | None = None) -> set[str]:
    """All addresses that belong to the account (lower-case).

    ICLOUD_USERNAME plus the comma-separated ICLOUD_OWN_ADDRESSES (aliases,
    custom-domain addresses). iCloud's IMAP has no way to list aliases, so they
    are configured.
    """
    addrs = {os.environ.get("ICLOUD_USERNAME", "")}
    addrs.update(os.environ.get("ICLOUD_OWN_ADDRESSES", "").split(","))
    addrs.update(extra or [])
    return {a.strip().lower() for a in addrs if a and a.strip()}


def reply_subject(original_subject: str) -> str:
    """Exactly one "Re:" in front, whatever reply prefixes the original carries."""
    subject = _unfold(original_subject)
    rest = _REPLY_PREFIX.sub("", subject).strip()
    return f"Re: {rest}" if rest else "Re:"


def _addresses(msg: email.message.Message, header: str) -> list[tuple[str, str]]:
    """(decoded display name, address) pairs of a header, folding removed."""
    raw = [_unfold(v) for v in msg.get_all(header, [])]
    pairs = []
    for name, addr in getaddresses(raw):
        if addr:
            pairs.append((decode_mime_header(name), addr))
    return pairs


def _format_addresses(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(formataddr((name, addr), charset="utf-8") for name, addr in pairs)


def _dedupe(pairs: list[tuple[str, str]], skip: set[str]) -> list[tuple[str, str]]:
    seen = set(skip)
    out = []
    for name, addr in pairs:
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((name, addr))
    return out


def reply_recipients(
    original: email.message.Message,
    own: set[str],
    original_is_own: bool = False,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
    """Return (to, cc, from_address) for a reply, the way Apple Mail picks them.

    * Reply to someone else's mail: To = Reply-To, else From. No Cc.
      From = the own address the mail was sent to.
    * Reply to an own mail (From is an own address, or the mail lies in a Sent
      folder): To = original To, Cc = original Cc, From = original From.
    * An own address is never a recipient.
    """
    sender = _addresses(original, "From")
    sender_addr = sender[0][1] if sender else ""
    own = set(own)
    if original_is_own and sender_addr:
        own.add(sender_addr.lower())

    if sender_addr.lower() in own:
        to = _dedupe(_addresses(original, "To"), own)
        cc = _dedupe(_addresses(original, "Cc"), own | {a.lower() for _, a in to})
        return to, cc, sender_addr

    reply_to = _addresses(original, "Reply-To") or sender
    to = _dedupe(reply_to, own)

    from_addr = ""
    for header in ("To", "Cc", "Delivered-To"):
        for _, addr in _addresses(original, header):
            if addr.lower() in own:
                from_addr = addr
                break
        if from_addr:
            break
    if not from_addr:
        delivered = _addresses(original, "Delivered-To") or _addresses(original, "To")
        from_addr = delivered[0][1] if delivered else os.environ.get("ICLOUD_USERNAME", "")
    return to, [], from_addr


def attribution_line(original: email.message.Message) -> str:
    """Apple Mail's line above the quote, date in Europe/Berlin.

    Example: "On 05.11.2026, at 09:26, Markus Lohmann <m@example.com> wrote:"
    """
    sender = _addresses(original, "From")
    if sender:
        name, addr = sender[0]
        who = f"{name} <{addr}>" if name else addr
    else:
        who = decode_mime_header(original.get("From", ""))
    raw_date = original.get("Date", "")
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        dt = dt.astimezone(TIMEZONE)
        when = dt.strftime("%d.%m.%Y, at %H:%M")
    except Exception:
        when = _unfold(raw_date)
    return f"On {when}, {who} wrote:"


# ----------------------------------------------------------------------
# Bodies
# ----------------------------------------------------------------------

def _decoded_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def body_parts(msg: email.message.Message) -> tuple[str, str]:
    """(plain, html) of a message; attachments are skipped."""
    plain = html_body = ""
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ct = part.get_content_type()
        if ct == "text/plain" and not plain:
            plain = _decoded_payload(part)
        elif ct == "text/html" and not html_body:
            html_body = _decoded_payload(part)
    return plain, html_body


def inline_images(msg: email.message.Message) -> dict[str, email.message.Message]:
    """Map Content-ID (without <>) -> image part, for parts with a Content-ID."""
    images = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        cid = (part.get("Content-ID") or "").strip().strip("<>").strip()
        if cid and part.get_content_maintype() == "image":
            images[cid] = part
    return images


_BLOCK_BREAK = re.compile(r"<\s*(br|/p|/div|/ul|/ol|/tr|/h[1-6]|/blockquote)\b[^>]*>", re.I)


def html_to_text(markup: str) -> str:
    """Readable plain text from HTML (paragraphs, line breaks, list bullets)."""
    text = re.sub(r"(?is)<(script|style|head|title)\b.*?</\1\s*>", "", markup)
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    text = re.sub(r"(?i)<\s*li\b[^>]*>", "\n• ", text)
    text = re.sub(r"(?i)<\s*p\b[^>]*>", "\n", text)
    text = _BLOCK_BREAK.sub("\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def body_text(msg: email.message.Message) -> str:
    """Plain text of a message, falling back to its HTML converted to text."""
    plain, html_body = body_parts(msg)
    if plain:
        return plain
    if html_body:
        return html_to_text(html_body)
    return ""


def text_to_html(text: str) -> str:
    """Apple Mail markup for typed text: a div per line, empty lines as <div><br></div>."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for line in lines:
        out.append(f"<div>{html.escape(line)}</div>" if line.strip() else "<div><br></div>")
    return "".join(out)


def _plain_quote_html(text: str) -> str:
    escaped = html.escape(text.replace("\r\n", "\n").replace("\r", "\n"))
    return escaped.replace("\n", "<br>")


_DANGEROUS_TAGS = re.compile(r"(?is)<(script|style|title)\b.*?</\1\s*>|<(script|style|meta|link|base)\b[^>]*/?>")
_EVENT_ATTR = re.compile(r"""(?i)\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""")


def html_inner_body(markup: str) -> str:
    """Content of <body> without <script>/<style>, ready to sit in a blockquote."""
    m = re.search(r"(?is)<body\b[^>]*>(.*)</body\s*>", markup)
    if m:
        inner = m.group(1)
    else:
        m = re.search(r"(?is)<body\b[^>]*>(.*)", markup)
        inner = m.group(1) if m else markup
        inner = re.sub(r"(?is)<head\b.*?</head\s*>", "", inner)
        inner = re.sub(r"(?is)</?html\b[^>]*>", "", inner)
    inner = _DANGEROUS_TAGS.sub("", inner)
    inner = _EVENT_ATTR.sub("", inner)
    return inner.strip()


_CID_REF = re.compile(r"""cid:([^"'\s>)]+)""", re.I)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)


def drop_missing_cid_images(markup: str, available: set[str]) -> tuple[str, set[str]]:
    """Remove <img> tags whose cid: has no image part; return (html, cids used)."""
    used: set[str] = set()

    def _img(m: re.Match) -> str:
        tag = m.group(0)
        ref = _CID_REF.search(tag)
        if not ref:
            return tag
        cid = ref.group(1)
        if cid in available:
            used.add(cid)
            return tag
        return ""

    markup = _IMG_TAG.sub(_img, markup)
    # Any other cid: reference (e.g. background) that stays must also resolve.
    for ref in _CID_REF.finditer(markup):
        if ref.group(1) in available:
            used.add(ref.group(1))
    return markup, used


def quoted_plain(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(f"> {line}" if line else ">" for line in lines)


# ----------------------------------------------------------------------
# Draft
# ----------------------------------------------------------------------

def _msgid_domain(address: str) -> str | None:
    return address.rsplit("@", 1)[1] if "@" in address else None


def build_reply(
    original: email.message.Message,
    reply_text: str = "",
    reply_html: str = "",
    own: set[str] | None = None,
    original_is_own: bool = False,
) -> tuple[email.message.Message, dict]:
    """Return (draft message, summary) for a reply to ``original``.

    The caller attaches files (if any) and appends the draft to Drafts.
    """
    own = own if own is not None else own_addresses()
    to, cc, from_addr = reply_recipients(original, own, original_is_own)

    # --- plain part -----------------------------------------------------
    typed_plain = reply_text if reply_text else html_to_text(reply_html)
    attribution = attribution_line(original)
    orig_plain, orig_html = body_parts(original)
    if not orig_plain and orig_html:
        orig_plain = html_to_text(orig_html)
    plain = f"{typed_plain}\n\n{attribution}\n\n{quoted_plain(orig_plain)}\n"

    # --- html part ------------------------------------------------------
    reply_markup = reply_html if reply_html else text_to_html(typed_plain)
    images = inline_images(original)
    used_cids: set[str] = set()
    if orig_html:
        quote_inner, used_cids = drop_missing_cid_images(html_inner_body(orig_html), set(images))
    else:
        quote_inner = _plain_quote_html(orig_plain)
    html_doc = (
        '<html><head><meta http-equiv="content-type" content="text/html; charset=utf-8"></head>'
        f'<body style="{_HTML_BODY_STYLE}">'
        f"{reply_markup}"
        '<div><br><blockquote type="cite">'
        f"<div>{html.escape(attribution)}</div>"
        '<br class="Apple-interchange-newline">'
        f"<div>{quote_inner}</div>"
        "</blockquote></div><br></body></html>"
    )

    html_part: email.message.Message = MIMEText(html_doc, "html", "utf-8")
    if used_cids:
        related = MIMEMultipart("related", type="text/html")
        related.attach(html_part)
        for cid in sorted(used_cids):
            img = copy.deepcopy(images[cid])
            # Keep the image inline; drop a filename-based "attachment" disposition.
            del img["Content-Disposition"]
            img.add_header("Content-Disposition", "inline")
            related.attach(img)
        html_part = related

    draft = MIMEMultipart("alternative")
    draft.attach(MIMEText(plain, "plain", "utf-8"))
    draft.attach(html_part)

    # --- headers --------------------------------------------------------
    subject = reply_subject(decode_mime_header(original.get("Subject", "")))
    orig_msg_id = _unfold(original.get("Message-ID", ""))
    references = _unfold(original.get("References", ""))
    if orig_msg_id and orig_msg_id not in references.split():
        references = f"{references} {orig_msg_id}".strip()

    draft["From"] = from_addr
    if to:
        draft["To"] = _format_addresses(to)
    if cc:
        draft["Cc"] = _format_addresses(cc)
    draft["Subject"] = Header(subject, "utf-8") if not subject.isascii() else subject
    draft["Date"] = formatdate(localtime=True)
    draft["Message-ID"] = make_msgid(domain=_msgid_domain(from_addr))
    if orig_msg_id:
        draft["In-Reply-To"] = orig_msg_id
    if references:
        draft["References"] = references

    summary = {
        "draft_to": _format_addresses(to) if to else "",
        "draft_cc": _format_addresses(cc) if cc else "",
        "draft_from": from_addr,
        "subject": subject,
        "inline_images": len(used_cids),
    }
    return draft, summary


def wrap_with_attachments(draft: email.message.Message, attachments: list[email.message.Message]) -> email.message.Message:
    """Put the alternative body and attachment parts into a multipart/mixed."""
    if not attachments:
        return draft
    mixed = MIMEMultipart("mixed")
    for key in ("From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To", "References"):
        if draft[key] is not None:
            mixed[key] = draft[key]
            del draft[key]
    del draft["MIME-Version"]
    mixed.attach(draft)
    for part in attachments:
        mixed.attach(part)
    return mixed

