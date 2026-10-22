"""Files the owner attaches to a message (hub #30):

  POST /api/chat/chats/{chat}/attachments?name=<file name>    the raw bytes as the body
       → {id, name, type, bytes}
  GET  /api/chat/chats/{chat}/attachments/{id}                the file, for the viewer

The body is the file itself, streamed to disk and stopped past 25 MB; no
multipart, so nothing is spooled before the cap applies. The server picks
the path; ``name`` is a display name only. A message then names its files
with ``attachments: [id]`` (``POST …/messages``). ``413 too_large``,
``415 unsupported_type``, ``429 too_many_unsent``, ``404`` for an unknown
chat or file, ``409 unsafe_attachments_folder`` when ``.xo`` or its
attachments folder is a link. Writes pass the browser write guard like
every other.

A file is served with headers that keep the browser from running it: text
always as ``text/plain``, ``nosniff``, a sandboxing CSP, and inline only
for images and PDFs.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import chats
from services.cowork_agent.storm.chats import attachments, rows
from services.errors import ServiceError

router = APIRouter()

#: Bytes handed to the writer thread at a time.
_BATCH = attachments.CHUNK


@router.post("/api/chat/chats/{chat_id}/attachments")
async def upload(chat_id: str, request: Request, name: Optional[str] = Query(None, max_length=1024)) -> dict:
    try:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > attachments.MAX_BYTES:
            raise attachments.AttachmentRefused(
                "too_large", f"A file may be at most {attachments.MAX_BYTES // (1024 * 1024)} MB.", status=413)
        service = chats.current()
        chat = await service.chat(chat_id)
        service.writable(chat)
        folder = attachments.chat_folder(chat)
        named = await service.engine.store.read(lambda conn: rows.attachment_ids(conn, chat_id))
        await asyncio.to_thread(attachments.check_room, folder, named, int(declared) if declared and
                                declared.isdigit() else None)
        return attachments.public(await _receive(request, attachments.Upload(
            folder=folder, chat_id=chat_id, name=attachments.clean_name(name))))
    except ServiceError as exc:
        raise storm_error(exc)


async def _receive(request: Request, upload: attachments.Upload) -> dict:
    await asyncio.to_thread(upload.open)
    try:
        pending = bytearray()
        async for chunk in request.stream():
            pending += chunk
            if len(pending) >= _BATCH:
                await asyncio.to_thread(upload.feed, bytes(pending))
                pending.clear()
        await asyncio.to_thread(upload.feed, bytes(pending))
        return await asyncio.to_thread(upload.finish)
    except BaseException:
        await asyncio.shield(asyncio.to_thread(upload.abort))
        raise


@router.get("/api/chat/chats/{chat_id}/attachments/{att_id}")
async def read(chat_id: str, att_id: str) -> StreamingResponse:
    try:
        chat = await chats.current().chat(chat_id)
        folder = attachments.chat_folder(chat)
        meta = await asyncio.to_thread(attachments.lookup, folder, att_id)
        fd = await asyncio.to_thread(attachments.open_file, folder, meta)
    except ServiceError as exc:
        raise storm_error(exc)
    size = os.fstat(fd).st_size
    return StreamingResponse(attachments.read_chunks(fd), media_type=_served_type(meta["type"]),
                             headers=_headers(meta, size))


def _served_type(kind: str) -> str:
    """Text is always plain text, whatever it holds (an SVG or HTML file is
    text and must never render as markup)."""
    return "text/plain; charset=utf-8" if kind.startswith("text/") else kind


def _headers(meta: dict, size: int) -> dict[str, str]:
    kind = meta["type"]
    disposition = "inline" if kind in attachments.INLINE_TYPES else "attachment"
    fallback = f"file.{attachments.TYPES[kind]}"
    # Chromium's PDF viewer refuses to render under a sandboxing CSP; a PDF
    # still loads nothing else and can't be framed by another site.
    csp = "default-src 'none'; frame-ancestors 'self'" if kind == "application/pdf" else \
        "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"
    return {
        "Content-Length": str(size),
        "Content-Disposition": f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(meta['name'], safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": csp,
        "Cross-Origin-Resource-Policy": "same-site",
        "Cache-Control": "private, max-age=86400, immutable",
    }
