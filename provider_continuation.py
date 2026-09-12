"""Durable provider metadata referenced by native Codex response-item IDs.

Codex owns conversation history. This store only keeps inference fields that its
Responses schema cannot represent, such as reasoning_content and thought signatures.
There are no credentials, tool execution, or agent sessions in this module.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid

LOCAL_ITEM_PREFIX = "mdkc_"
ASSISTANT_METADATA_FIELDS = ("reasoning_content", "reasoning", "reasoning_details", "extra_content")


class ContinuationError(ValueError):
    """Display-safe continuation failure; never includes stored reasoning or signatures."""


def continuation_scope(endpoint, model):
    identity = [endpoint["base_url"].rstrip("/"), endpoint.get("account"), model]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]


def is_local_item_id(item_id):
    return isinstance(item_id, str) and item_id.startswith(LOCAL_ITEM_PREFIX)


def new_item_id(scope):
    return f"{LOCAL_ITEM_PREFIX}{scope}_{uuid.uuid4().hex}"


def response_item_id(scope, original, response_nonce=""):
    suffix = hashlib.sha256(json.dumps([response_nonce, original], sort_keys=True).encode()).hexdigest()
    return f"{LOCAL_ITEM_PREFIX}{scope}_{suffix}"


def _item_identity(item):
    """Compare the visible content before attaching its original provider metadata."""
    kind = item.get("type")
    if kind == "message":
        content = [{key: value for key, value in part.items() if key in ("type", "text", "image_url", "detail")}
                   for part in item.get("content") or [] if isinstance(part, dict)]
        return {"type": kind, "role": item.get("role"), "content": content}
    if kind == "function_call":
        arguments = item.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            pass
        return {"type": kind, "name": item.get("name"), "call_id": item.get("call_id"), "arguments": arguments}
    if kind == "reasoning":
        summary = copy.deepcopy(item.get("summary") or [])
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                summary.append({"type": "summary_text", "text": part["text"]})
        return {"type": kind, "summary": summary}
    raise ContinuationError("This provider returned an unsupported continuation item.")


class ProviderContinuationStore:
    """One atomic transaction per completed inference; safe for concurrent router threads."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_status = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent_status.st_mode) or parent_status.st_uid != os.getuid()
                or parent_status.st_mode & 0o022):
            raise ContinuationError("The provider continuation folder must be private and owned by the current user.")
        if self.path.is_symlink() or any(Path(str(self.path) + suffix).is_symlink() for suffix in ("-journal", "-wal", "-shm")):
            raise ContinuationError("The local provider continuation store is not a regular file.")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode) or opened_status.st_uid != os.getuid() or opened_status.st_nlink != 1:
                raise ContinuationError("The local provider continuation store is not a private regular file.")
            os.fchmod(descriptor, 0o600)
            connection = sqlite3.connect(self.path, timeout=5)
            current_status = self.path.lstat()
            if (current_status.st_dev, current_status.st_ino) != (opened_status.st_dev, opened_status.st_ino):
                connection.close()
                raise ContinuationError("The local provider continuation store changed while opening it.")
        finally:
            os.close(descriptor)
        connection.execute("CREATE TABLE IF NOT EXISTS continuation "
                           "(item_id TEXT PRIMARY KEY, scope TEXT NOT NULL, response_id TEXT NOT NULL, "
                           "identity TEXT NOT NULL, metadata TEXT NOT NULL)")
        return connection

    def save(self, scope, response_id, records):
        """Persist all item metadata before any corresponding output_item.done event."""
        rows = []
        for item, metadata in records:
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id.startswith(f"{LOCAL_ITEM_PREFIX}{scope}_"):
                raise ContinuationError("The provider continuation identifier does not match its connection.")
            rows.append((item_id, scope, response_id, json.dumps(_item_identity(item), sort_keys=True),
                         json.dumps(metadata, sort_keys=True, separators=(",", ":"))))
        if not rows:
            return
        try:
            with self.lock:
                connection = self._connect()
                try:
                    with connection:
                        for row in rows:
                            existing = connection.execute("SELECT * FROM continuation WHERE item_id = ?", (row[0],)).fetchone()
                            if existing is not None and tuple(existing) != row:
                                raise ContinuationError("The provider attempted to replace an existing continuation record.")
                            connection.execute("INSERT OR IGNORE INTO continuation VALUES (?, ?, ?, ?, ?)", row)
                finally:
                    connection.close()
        except (sqlite3.Error, OSError):
            raise ContinuationError("Provider continuation could not be saved locally. The response was not completed.") from None

    def load(self, scope, item):
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.startswith(f"{LOCAL_ITEM_PREFIX}{scope}_"):
            return None  # Foreign-provider metadata must never cross a routing boundary.
        if not self.path.is_file():
            raise ContinuationError("Required provider continuation is missing. Restore Model Deck's continuation store or start a new task.")
        try:
            with self.lock:
                connection = self._connect()
                try:
                    row = connection.execute("SELECT response_id, identity, metadata FROM continuation "
                                             "WHERE item_id = ? AND scope = ?", (item_id, scope)).fetchone()
                finally:
                    connection.close()
            if row is None:
                raise ContinuationError("Required provider continuation is missing. Restore Model Deck's continuation store or start a new task.")
            if json.loads(row[1]) != _item_identity(item):
                raise ContinuationError("This task's provider history changed and its continuation cannot be replayed safely. Start a new task.")
            return {"response_id": row[0], "metadata": json.loads(row[2])}
        except (sqlite3.Error, OSError, ValueError) as error:
            if isinstance(error, ContinuationError):
                raise
            raise ContinuationError("The local provider continuation store could not be read.") from None

    def capture_response_item(self, scope, item, response_nonce=""):
        """Retain a direct Responses provider's own opaque fields without inventing any."""
        original = copy.deepcopy(item)
        local_id = response_item_id(scope, original.get("id") or json.dumps(original, sort_keys=True), response_nonce)
        visible = dict(original, id=local_id)
        self.save(scope, local_id, [(visible, {"response_item": original})])
        return local_id

    def restore_responses_input(self, scope, items):
        restored = []
        for item in items:
            record = self.load(scope, item)
            if record is not None:
                original = record["metadata"].get("response_item")
                if not isinstance(original, dict):
                    raise ContinuationError("The connection's inference format changed. Start a new task for this provider.")
                restored.append(copy.deepcopy(original))
            elif item.get("type") != "reasoning":
                visible = copy.deepcopy(item)
                visible.pop("id", None)
                restored.append(visible)
        return restored
