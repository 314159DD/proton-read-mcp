#!/usr/bin/env python3
"""
Minimal READ-ONLY Proton Mail MCP server.

Reads mail from Proton Mail via Proton Bridge (local IMAP) and exposes it to
Claude as MCP tools.

Read-only by construction:
  - There are NO tools that send, reply, delete, move, or modify anything.
  - Every fetch uses BODY.PEEK, so reading does not even mark a message as seen.
  - Folders are opened with readonly=True.
Sending is intentionally out of scope. This server is only the eyes.

Backend : Proton Bridge IMAP (default 127.0.0.1:1143, STARTTLS)
Transport: stdio MCP (works with Claude Desktop and Claude Code)
Deps     : just `mcp`. IMAP + parsing use the Python standard library.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import ssl
from email.header import decode_header, make_header

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# Configuration (env vars; secrets may be passed via *_FILE to avoid plaintext)
# --------------------------------------------------------------------------

def _secret(name: str) -> str:
    val = os.environ.get(name)
    if val:
        return val
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return ""

USERNAME = _secret("PROTONMAIL_USERNAME")
PASSWORD = _secret("PROTONMAIL_PASSWORD")          # Bridge password, NOT the Proton login
HOST = os.environ.get("PROTONMAIL_IMAP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROTONMAIL_IMAP_PORT", "1143"))

mcp = FastMCP("proton-read")

# --------------------------------------------------------------------------
# IMAP helpers
# --------------------------------------------------------------------------

def _connect() -> imaplib.IMAP4:
    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "PROTONMAIL_USERNAME / PROTONMAIL_PASSWORD not set "
            "(use PROTONMAIL_PASSWORD_FILE for the Bridge password)."
        )
    # Proton Bridge serves a local self-signed cert on localhost, so we relax
    # verification. Traffic stays on 127.0.0.1 (or an SSH tunnel), never the open net.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = imaplib.IMAP4(HOST, PORT)
    conn.starttls(ssl_context=ctx)
    conn.login(USERNAME, PASSWORD)
    return conn

def _logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except Exception:
        pass

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value

def _imap_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def _flags_from(blob: bytes | None) -> bytes:
    if not blob:
        return b""
    # Bridge/IMAP put FLAGS in the FETCH preamble, e.g. b'1 (UID 5 FLAGS (\\Seen) ...'.
    # Whitespace inside the parens varies; match case-insensitively to be safe.
    m = re.search(rb"FLAGS\s*\(([^)]*)\)", blob, re.IGNORECASE)
    return m.group(1) if m else b""

def _is_seen(flags: bytes) -> bool:
    # IMAP flags are case-insensitive per RFC 3501; compare lowercased.
    return b"\\seen" in flags.lower()

def _folder_name(line: bytes) -> str:
    """Extract the mailbox name from one LIST response line.

    Bridge returns lines like: (\\HasNoChildren) "/" "INBOX" — the name is the
    last quoted token (may contain spaces or an escaped quote). Falls back to the
    last whitespace-separated atom for servers that return unquoted names.
    """
    decoded = line.decode(errors="replace")
    # Trailing quoted string, allowing backslash-escaped quotes inside.
    m = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', decoded)
    if m:
        return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    # Unquoted atom fallback: take the last token after the hierarchy separator.
    return decoded.strip().split()[-1] if decoded.strip() else decoded

def _overview(conn: imaplib.IMAP4, uid: bytes) -> dict:
    """Fast metadata for one message: id, from, to, subject, date, unread."""
    uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
    typ, data = conn.uid(
        "FETCH", uid_s,
        "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
    )
    headers, flags, preamble = b"", b"", b""
    for part in data:
        if isinstance(part, tuple):
            # part[0] is the preamble bytes (UID/FLAGS/literal marker); part[1] the literal.
            preamble += bytes(part[0] or b"")
            flags = flags or _flags_from(part[0])
            headers = part[1] or headers
        elif isinstance(part, (bytes, bytearray)):
            preamble += bytes(part)
            flags = flags or _flags_from(part)
    # Fallback: flags may sit in a chunk we didn't attribute above.
    flags = flags or _flags_from(preamble)
    msg = email.message_from_bytes(headers) if headers else email.message_from_string("")
    return {
        "id": uid_s,
        "from": _decode(msg.get("From")),
        "to": _decode(msg.get("To")),
        "subject": _decode(msg.get("Subject")),
        "date": msg.get("Date", ""),
        "unread": not _is_seen(flags),
    }

def _payload_text(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return ""

def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    # Turn block-level breaks into newlines so paragraphs survive as text.
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(a, b)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _extract_body(msg) -> tuple[str, list[dict]]:
    body, html, attachments = "", "", []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = str(part.get("Content-Disposition") or "").lower()
            ctype = part.get_content_type()
            if "attachment" in disp or part.get_filename():
                attachments.append({
                    "filename": _decode(part.get_filename()) or "(unnamed)",
                    "content_type": ctype,
                })
                continue
            if ctype == "text/plain" and not body:
                body = _payload_text(part)
            elif ctype == "text/html" and not html:
                html = _payload_text(part)
    else:
        if msg.get_content_type() == "text/html":
            html = _payload_text(msg)
        else:
            body = _payload_text(msg)
    if not body and html:
        body = _strip_html(html)
    return body.strip(), attachments

# --------------------------------------------------------------------------
# Tools (all read-only)
# --------------------------------------------------------------------------

@mcp.tool()
def list_folders() -> list[dict]:
    """List all mailbox folders and labels in the Proton account."""
    conn = _connect()
    try:
        typ, data = conn.list()
        out = []
        for line in data or []:
            if not line:
                continue
            out.append({"folder": _folder_name(line)})
        return out
    finally:
        _logout(conn)

@mcp.tool()
def list_recent(folder: str = "INBOX", limit: int = 20) -> list[dict]:
    """List the most recent emails in a folder, newest first.

    Returns lightweight metadata (id, from, to, subject, date, unread). Use the
    returned id with read_email to get the full body. Does not mark anything read.
    """
    limit = max(1, min(limit, 100))
    conn = _connect()
    try:
        conn.select(folder, readonly=True)
        typ, data = conn.uid("SEARCH", "ALL")
        uids = data[0].split() if data and data[0] else []
        uids = uids[-limit:][::-1]
        return [_overview(conn, u) for u in uids]
    finally:
        _logout(conn)

@mcp.tool()
def search_emails(query: str, folder: str = "INBOX", limit: int = 20) -> list[dict]:
    """Search a folder by free text (matches the whole message: sender, subject, body).

    Newest first. Read-only. Note: matching is ASCII-oriented; for names with
    umlauts, try a plain substring like 'Mueller' or search by company domain.
    """
    limit = max(1, min(limit, 100))
    conn = _connect()
    try:
        conn.select(folder, readonly=True)
        try:
            typ, data = conn.uid("SEARCH", "TEXT", _imap_quote(query))
        except imaplib.IMAP4.error:
            return [{"error": f"search failed for query: {query!r}"}]
        uids = data[0].split() if data and data[0] else []
        uids = uids[-limit:][::-1]
        return [_overview(conn, u) for u in uids]
    finally:
        _logout(conn)

@mcp.tool()
def read_email(email_id: str, folder: str = "INBOX") -> dict:
    """Read one email's full content by the id returned from list_recent / search_emails.

    Returns headers, the plain-text body (HTML is stripped to text), and a list of
    attachment names (content is not downloaded). Read-only: does not mark it read.
    """
    conn = _connect()
    try:
        conn.select(folder, readonly=True)
        typ, data = conn.uid("FETCH", str(email_id), "(FLAGS BODY.PEEK[])")
        raw, flags, preamble = None, b"", b""
        for part in data:
            if isinstance(part, tuple):
                preamble += bytes(part[0] or b"")
                flags = flags or _flags_from(part[0])
                raw = part[1] or raw
            elif isinstance(part, (bytes, bytearray)):
                preamble += bytes(part)
                flags = flags or _flags_from(part)
        flags = flags or _flags_from(preamble)
        if not raw:
            return {"error": f"email {email_id} not found in {folder}"}
        msg = email.message_from_bytes(raw)
        body, attachments = _extract_body(msg)
        return {
            "id": str(email_id),
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "cc": _decode(msg.get("Cc")),
            "subject": _decode(msg.get("Subject")),
            "date": msg.get("Date", ""),
            "unread": not _is_seen(flags),
            "body": body[:20000],
            "attachments": attachments,
        }
    finally:
        _logout(conn)

@mcp.tool()
def unread_count(folder: str = "INBOX") -> dict:
    """Count unread and total messages in a folder."""
    conn = _connect()
    try:
        conn.select(folder, readonly=True)
        t1, d1 = conn.uid("SEARCH", "UNSEEN")
        t2, d2 = conn.uid("SEARCH", "ALL")
        unread = len(d1[0].split()) if d1 and d1[0] else 0
        total = len(d2[0].split()) if d2 and d2[0] else 0
        return {"folder": folder, "unread": unread, "total": total}
    finally:
        _logout(conn)


def _check() -> int:
    """Read-only connectivity self-test against Proton Bridge.

    Connects via _connect(), lists folders and reports INBOX unread/total,
    prints a short human summary to stdout. Returns 0 on success, 1 on failure.
    No writes: folder list + counts only, same BODY.PEEK / readonly semantics.
    """
    try:
        conn = _connect()
    except Exception as exc:
        print(f"FAIL: could not connect to Bridge at {HOST}:{PORT}: {exc}")
        return 1
    try:
        typ, data = conn.list()
        folders = [_folder_name(line) for line in (data or []) if line]
        conn.select("INBOX", readonly=True)
        t1, d1 = conn.uid("SEARCH", "UNSEEN")
        t2, d2 = conn.uid("SEARCH", "ALL")
        unread = len(d1[0].split()) if d1 and d1[0] else 0
        total = len(d2[0].split()) if d2 and d2[0] else 0
        print(f"OK: connected to Bridge at {HOST}:{PORT} as {USERNAME}")
        print(f"Folders ({len(folders)}): " + ", ".join(folders))
        print(f"INBOX: {unread} unread / {total} total")
        return 0
    except Exception as exc:
        print(f"FAIL: connected but query failed: {exc}")
        return 1
    finally:
        _logout(conn)


def main() -> None:
    import sys
    if "--check" in sys.argv:
        raise SystemExit(_check())
    # FastMCP defaults to stdio transport.
    mcp.run()


if __name__ == "__main__":
    main()
