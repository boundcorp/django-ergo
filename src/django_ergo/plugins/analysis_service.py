# ruff: noqa: TRY003, EM101, PLR2004, C901
"""Session authorization, lazy receipts and streaming attachment orchestration.

Only HTTP and Django storage run here. Parsing and file operations are jobs.
"""

import base64
import hashlib
import json
import re
import tempfile
import time
import uuid
from contextlib import contextmanager

from django.core import signing
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from django_ergo.analysis.client import configured_client
from django_ergo.analysis.models import AnalysisDataset
from django_ergo.analysis.models import AnalysisJob
from django_ergo.analysis.models import AnalysisWorkspace
from django_ergo.analysis.protocol import CHUNK_BYTES
from django_ergo.analysis.protocol import MAX_FILE_BYTES
from django_ergo.analysis.protocol import ProtocolError
from django_ergo.analysis.protocol import encode
from django_ergo.analysis.protocol import identifier
from django_ergo.analysis.query import compile_select
from django_ergo.analysis.results import dataset_result
from django_ergo.analysis.results import select_result
from django_ergo.analysis.runner import RunnerUnavailableError
from django_ergo.bots.registry import closed_to
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession

TERMINAL = {"succeeded", "failed", "cancelled", "timeout"}
CURSOR_SALT = "django-ergo-analysis-v1"


def safe_name(name):
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", name)
        or ".." in name
    ):
        raise ValueError("Use a flat workspace filename without traversal")
    return name


class AnalysisService:
    def __init__(self, ctx, *, client=None):
        self.ctx = ctx
        self.client = configured_client() if client is None else client
        self.authorize()

    def authorize(self):
        # Reload: a stale ToolContext is never an authorization grant.
        uid = getattr(self.ctx.user, "pk", None)
        sid = getattr(self.ctx.session, "pk", None)
        session = ConversationSession.objects.filter(pk=sid, user_id=uid).first()
        if session is None or self.ctx.bot is None or closed_to(self.ctx.bot, session):
            raise PermissionError("No authorized analysis session")
        self.ctx.session = session
        self.session = session
        return session

    def workspace(self):
        session = self.authorize()
        with transaction.atomic():
            # Session row serializes lazy creation even before the workspace exists.
            ConversationSession.objects.select_for_update().get(pk=session.pk)
            workspace, _ = AnalysisWorkspace.objects.get_or_create(
                session=session, retired=False
            )
        return workspace

    def owned_workspace(self, wid):
        session = self.authorize()
        row = AnalysisWorkspace.objects.filter(
            pk=identifier(str(wid)), session=session, retired=False
        ).first()
        if row is None:
            raise PermissionError("No workspace in this session")
        return row

    def call(self, workspace, action, **kwargs):
        self.owned_workspace(workspace.id)
        body = {
            "workspace": str(workspace.id),
            "session": str(self.session.id),
            "user": str(self.session.user_id),
            "action": action,
            **kwargs,
        }
        AnalysisWorkspace.objects.filter(pk=workspace.id).update(
            touched_at=timezone.now()
        )
        response = self.client.call(body)
        if response.get("state") == "retired":
            AnalysisWorkspace.objects.filter(pk=workspace.id).update(retired=True)
            raise RunnerUnavailableError("Workspace expired; retry to create a new one")
        return response

    def submit(self, request, *, workspace=None, lease="", metadata=None):
        request_bytes = encode(request, limit=64 * 1024)
        workspace = (
            self.workspace()
            if workspace is None
            else self.owned_workspace(workspace.id)
        )
        job = AnalysisJob.objects.create(
            workspace=workspace,
            operation=request["op"],
            request=request_bytes,
            metadata={**(metadata or {}), **({"lease": lease} if lease else {})},
        )
        # Persist identity before HTTP: ambiguous timeouts can be polled by receipt.
        try:
            response = self.call(
                workspace, "submit", job=str(job.id), request=request, lease=lease
            )
        except RunnerUnavailableError:
            return {
                "job": str(job.id),
                "state": "submitting",
                "notice": "Submission uncertain; poll this job before retrying",
            }
        job.state = response["state"]
        job.save(update_fields=["state"])
        return response

    def status(self, job_id, *, cancel=False):
        self.authorize()
        job = (
            AnalysisJob.objects.select_related("workspace")
            .filter(
                pk=identifier(job_id),
                workspace__session=self.session,
                workspace__retired=False,
            )
            .first()
        )
        if job is None:
            raise PermissionError("No job in this session")
        if job.state == "submitting" and not cancel:
            try:
                result = self.call(
                    job.workspace,
                    "submit",
                    job=str(job.id),
                    request=json.loads(bytes(job.request)),
                    lease=job.metadata.get("lease", ""),
                )
            except RunnerUnavailableError:
                return {"job": str(job.id), "state": "submitting"}
        else:
            result = self.call(
                job.workspace,
                "cancel" if cancel else "status",
                job=str(job.id),
                **(
                    {"request": json.loads(bytes(job.request))}
                    if cancel and job.state == "submitting"
                    else {}
                ),
            )
        job.state = result["state"]
        job.save(update_fields=["state"])
        if job.state == "succeeded":
            response = result.get("response", {})
            if response.get("ok") is not True or not isinstance(
                response.get("result"), dict
            ):
                raise ProtocolError("Malformed executor result")
            value = response["result"]
            if job.operation == "dataset":
                dataset_result(value)
                row, _ = AnalysisDataset.objects.get_or_create(
                    job=job,
                    defaults={
                        "workspace": job.workspace,
                        "filename": safe_name(value["file"]),
                        "version": value["version"],
                        "schema": value["schema"],
                        "rows": value["rows"],
                    },
                )
                result["response"]["result"] = {
                    "dataset": str(row.id),
                    "version": row.version,
                    "schema": row.schema,
                    "rows": row.rows,
                }
            if job.operation == "select":
                if value.get("version") != job.metadata["version"]:
                    raise ProtocolError("Dataset version mismatch")
                dataset = AnalysisDataset.objects.get(
                    pk=job.metadata["dataset"], workspace=job.workspace
                )
                request = json.loads(bytes(job.request))
                compiled = compile_select(
                    request["query"], [column["name"] for column in dataset.schema]
                )
                select_result(
                    value,
                    columns=compiled.columns,
                    source_rows=dataset.rows,
                    offset=job.metadata["offset"],
                    limit=request["query"].get("limit", 100),
                )
                offset = value.pop("next_offset", None)
                if offset is not None:
                    if type(offset) is not int or offset <= job.metadata["offset"]:
                        raise ProtocolError("Invalid pagination result")
                    value["cursor"] = signing.dumps(
                        {
                            **job.metadata,
                            "workspace": str(job.workspace_id),
                            "offset": offset,
                        },
                        salt=CURSOR_SALT,
                        compress=True,
                    )
                else:
                    value["cursor"] = None
        return result

    def select(self, dataset_id, query, cursor=""):
        self.authorize()
        dataset = (
            AnalysisDataset.objects.select_related("workspace")
            .filter(
                pk=identifier(dataset_id),
                workspace__session=self.session,
                workspace__retired=False,
            )
            .first()
        )
        if dataset is None:
            raise PermissionError("No dataset in this session")
        query_hash = hashlib.sha256(encode(query)).hexdigest()
        metadata = {
            "dataset": str(dataset.id),
            "version": dataset.version,
            "query": query_hash,
            "offset": 0,
        }
        if cursor:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError("Invalid cursor")
            payload = signing.loads(cursor, salt=CURSOR_SALT, max_age=86400)
            if (
                {k: payload.get(k) for k in ("dataset", "version", "query")}
                != {k: metadata[k] for k in ("dataset", "version", "query")}
                or payload.get("workspace") != str(dataset.workspace_id)
                or type(payload.get("offset")) is not int
            ):
                raise PermissionError("Cursor does not bind dataset, session and query")
            metadata["offset"] = payload["offset"]
        return self.submit(
            {
                "op": "select",
                "file": dataset.filename,
                "version": dataset.version,
                "query": query,
                "offset": metadata["offset"],
            },
            workspace=dataset.workspace,
            metadata=metadata,
        )

    def wait(self, submitted):
        # Transfers wait only on isolated jobs; no local parser or code fallback.
        job = submitted["job"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = self.status(job)
            if result["state"] in TERMINAL:
                if (
                    result["state"] != "succeeded"
                    or result["response"].get("ok") is not True
                ):
                    raise RunnerUnavailableError("Isolated transfer failed")
                return result["response"]["result"]
            time.sleep(0.25)
        self.status(job, cancel=True)
        raise RunnerUnavailableError(
            "Transfer timed out; workspace remains fenced until reaped"
        )

    @contextmanager
    def transfer(self):
        workspace, lease = self.workspace(), str(uuid.uuid4())
        self.call(workspace, "acquire", lease=lease)
        try:
            yield workspace, lease
        finally:
            # Release refusal deliberately leaves a bounded lease if a job is still active.
            self.call(workspace, "release", lease=lease)

    def import_attachment(self, attachment_id):
        from django_ergo.plugins.attachments import AttachmentsPlugin

        # Same source ACL as attachments, including closed/private cross-session rules.
        self.authorize()
        lookup = getattr(self.ctx.bot, "plugin", None)
        plugin = lookup("attachments") if callable(lookup) else None
        if plugin is None:
            plugin = AttachmentsPlugin(self.ctx.bot, {})
            plugin.on_load()
        row = plugin.file_for(self.ctx, attachment_id)
        # Reusable arbitrary-code workspaces can transform bytes and export new
        # attachments. Until derived-file privacy provenance exists, refusing
        # protected copies prevents laundering their no-forward restriction.
        if row.private_export_id:
            raise PermissionError(
                "Private export copies cannot enter reusable analysis workspaces"
            )
        if not row.file or (row.size is not None and row.size > MAX_FILE_BYTES):
            raise ValueError("Stored attachment required; file exceeds transfer cap")
        extension = row.filename.rsplit(".", 1)[-1].lower()
        if not re.fullmatch(r"[a-z0-9]{1,12}", extension):
            extension = "bin"
        name = "import-" + str(uuid.uuid4()) + "." + extension
        with self.transfer() as (workspace, lease), row.file.open("rb") as source:
            offset = 0
            checksum = hashlib.sha256()
            while True:
                # Recheck source privacy/ownership on every bounded stream read.
                self.authorize()
                current = plugin.file_for(self.ctx, str(row.id))
                if (
                    current.private_export_id
                    or current.file.name != row.file.name
                    or current.sha256 != row.sha256
                    or current.updated_at != row.updated_at
                ):
                    raise ValueError("Attachment changed during import")
                chunk = source.read(CHUNK_BYTES)
                if offset + len(chunk) > MAX_FILE_BYTES:
                    raise ValueError("Attachment stream exceeds cap")
                if not chunk and offset:
                    break
                result = self.wait(
                    self.submit(
                        {
                            "op": "write",
                            "file": name,
                            "offset": offset,
                            "data": base64.b64encode(chunk).decode(),
                        },
                        workspace=workspace,
                        lease=lease,
                    )
                )
                offset += len(chunk)
                checksum.update(chunk)
                if result.get("bytes") != offset:
                    raise ProtocolError("Invalid write acknowledgement")
                if not chunk:
                    break
            if row.sha256 and checksum.hexdigest() != row.sha256:
                raise ValueError("Attachment checksum mismatch")
        return {
            "file": name,
            "bytes": offset,
            "source_attachment": str(row.id),
            "sha256": checksum.hexdigest(),
        }

    def export_file(self, name, filename=""):
        name = safe_name(name)
        display_name = safe_name(filename or name)
        # Spools bytes only; never parses file content in the app.
        with self.transfer() as (workspace, lease), tempfile.TemporaryFile() as target:
            offset, version, size = 0, "", None
            while True:
                value = self.wait(
                    self.submit(
                        {
                            "op": "read",
                            "file": name,
                            "offset": offset,
                            "version": version,
                        },
                        workspace=workspace,
                        lease=lease,
                    )
                )
                chunk = base64.b64decode(value["data"], validate=True)
                if (
                    len(chunk) > CHUNK_BYTES
                    or type(value.get("size")) is not int
                    or not 0 <= value["size"] <= MAX_FILE_BYTES
                    or value.get("offset") != offset
                    or not re.fullmatch(r"[a-f0-9]{64}", value.get("version", ""))
                ):
                    raise ProtocolError("Invalid export chunk")
                if size is not None and (
                    size != value["size"] or version != value["version"]
                ):
                    raise ProtocolError("Export changed during streaming")
                size, version = value["size"], value["version"]
                offset += len(chunk)
                if offset > size or (not chunk and offset < size):
                    raise ProtocolError("Incomplete export stream")
                target.write(chunk)
                if offset == size:
                    break
            target.seek(0)
            if hashlib.file_digest(target, "sha256").hexdigest() != version:
                raise ProtocolError("Export checksum mismatch")
            target.seek(0)
            self.authorize()
            media_type = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
            }.get(display_name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
            attachment = ConversationAttachment(
                session=self.session,
                source="bot",
                kind="image" if media_type.startswith("image/") else "document",
                media_type=media_type,
                filename=display_name,
                size=size,
                sha256=version,
            )
            attachment.file.save(display_name, File(target), save=False)
            try:
                attachment.save()
            except Exception:
                attachment.file.delete(save=False)
                raise
        return {
            "attachment": str(attachment.id),
            "filename": display_name,
            "bytes": size,
        }
