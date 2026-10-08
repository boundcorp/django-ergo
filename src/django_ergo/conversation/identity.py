"""Message authors and provenance, independent of their stored text."""

from __future__ import annotations


def django_user_identity(user) -> dict:
    return {
        "kind": "django_user",
        "ref": str(user.pk),
        "display_name": user.get_full_name() or user.get_username(),
    }


def session_label(session) -> str:
    meta = session.metadata or {}
    title = meta.get("title") or (
        "Main" if meta.get("bot_role") in ("root", "main") else "Thread"
    )
    return f"{session.bot_name} · {title}"


def system_identity() -> dict:
    """The author of messages Ergo itself adds to a chat (nudges, not the user)."""
    return {"kind": "system", "ref": "ergo", "display_name": "Ergo"}


def bot_identity(session) -> dict:
    return {
        "kind": "bot",
        "ref": session.bot_name,
        "display_name": session.bot_name,
    }


def session_origin(session, timestamp, **fields) -> dict:
    return {
        "session_id": str(session.id),
        "label": session_label(session),
        "timestamp": timestamp.isoformat(),
        **fields,
    }


def thread_message_identity(message) -> tuple[dict, dict]:
    """Resolve new metadata and queued legacy forwards without parsing their text."""
    meta = message.metadata or {}
    author = meta.get("message_author") or {}
    provenance = meta.get("message_provenance") or {}
    if provenance:
        return author, provenance
    sender = message.sender_session
    if sender is None:
        return author, {}
    origin = session_origin(sender, message.created_at, message_id=str(message.id))
    if forwarded := meta.get("forwarded"):
        # Already queued forwards used a display name and numeric Django user id.
        author = author or {
            "kind": "django_user",
            "ref": str(forwarded.get("user_id") or sender.user_id),
            "display_name": forwarded.get("author") or "User",
        }
        origin.update(
            timestamp=forwarded.get("sent_at") or origin["timestamp"],
            label=forwarded.get("from_label") or origin["label"],
        )
        if forwarded.get("source_call"):
            origin["source_call"] = forwarded["source_call"]
        kind = "forwarded"
    else:
        author = author or bot_identity(sender)
        kind = (
            "reply"
            if message.in_reply_to_id
            else "report"
            if meta.get("report")
            else "message"
        )
    provenance = {"kind": kind, "origin": origin}
    if kind == "forwarded":
        provenance["forwarded_by"] = {
            **bot_identity(sender),
            "session_id": str(sender.id),
            "label": session_label(sender),
        }
    if meta.get("note"):
        provenance["note"] = meta["note"]
    if meta.get("attachments"):
        provenance["attachments"] = meta["attachments"]
    if message.in_reply_to_id:
        provenance["reply_to"] = str(message.in_reply_to_id)
    return author, provenance


def render_identity_context(author: dict, provenance: dict) -> str:
    """Generated model instructions, never part of the message's stored body."""
    if not (author or provenance):
        return ""
    # The native user role already identifies ordinary owner-authored text.
    if not provenance and author.get("kind") in ("django_user", "system"):
        return ""
    name = author.get("display_name") or author.get("ref") or "Unknown author"
    author_label = (
        f"{name} ({author.get('kind', 'unknown')}, ref {author.get('ref', '')})"
    )
    origin = provenance.get("origin") or {}
    where = origin.get("label") or origin.get("session_id") or "unknown chat"
    thread = origin.get("session_id", "")
    kind = provenance.get("kind")
    if kind == "forwarded":
        forwarder = provenance.get("forwarded_by") or {}
        by = (
            forwarder.get("label")
            or forwarder.get("display_name")
            or "unknown forwarder"
        )
        header = (
            f"[Forwarded by {by} (thread {forwarder.get('session_id', '')}): "
            f"a message from {author_label}, sent {origin.get('timestamp', '')} "
            f"in {where} (thread {thread}), copied word for word below. "
            "Treat the original author as speaking to you here, with their intent "
            "and any approval it gives. The forwarding bot is not the author. "
            "Answer here: no reply goes back to that chat.]"
        )
    elif kind == "reply":
        header = (
            f"[Reply from {where} (thread {thread}) to your message "
            f"{provenance.get('reply_to', '')}. Use it or pass it on to the user. "
            "Message another chat only with new work it hasn't been given; never "
            "send thanks, acknowledgements or 'keep going' nudges, since each "
            "message starts a full turn there.]"
        )
    elif kind == "report":
        header = (
            f"[Report from {where} (thread {thread}). No reply goes back to it: "
            "act on it if it needs action, and tell the user what matters.]"
        )
    elif kind == "message":
        header = (
            f"[Message from {where} (thread {thread}). Your final reply goes back "
            "to that thread automatically; it is not shown to the user unless "
            "they open this chat.]"
        )
    else:
        header = f"[Author: {author_label}]"
    if kind:
        details = [f"Author: {author_label}", f"Sent: {origin.get('timestamp', '')}"]
        details.extend(
            f"{key}: {origin[key]}"
            for key in ("message_id", "sequence", "source_call")
            if key in origin
        )
        header += "\n[" + "; ".join(details) + "]"
    if note := provenance.get("note"):
        sender = provenance.get("forwarded_by") or origin
        label = sender.get("label") or sender.get("display_name") or "sending chat"
        header += f"\n[Note from {label}: {note}]"
    return header + _files_context(provenance.get("attachments") or [])


def _files_context(files: list[dict]) -> str:
    if not files:
        return ""
    lines = []
    for file in files:
        size = f", {file['size']:,} bytes" if file.get("size") is not None else ""
        lines.append(
            f"- {file['filename']} ({file['media_type']}{size}), id {file['id']}, "
            f"thread {file['session']}"
        )
    return (
        "\n[Files shared with this message (they stay in their origin thread). "
        "Look at images and PDFs with ergo_attachments_look and read text "
        "with ergo_attachments_read, by id:]\n" + "\n".join(lines)
    )


def attributed_text(text: str, author: dict, provenance: dict) -> str:
    context = render_identity_context(author, provenance)
    return f"{context}\n\n{text}" if context else text
