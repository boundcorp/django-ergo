# ruff: noqa: INP001 -- standalone harness module next to e2e.py
"""Recursive inotify write audit (Linux, stdlib ctypes only).

Records every create/modify/close-write/move/attrib/delete event under a root,
including files created and removed again, which a before/after hash diff misses.
"""

import ctypes
import ctypes.util
import os
import select
import struct
from pathlib import Path

IN_MODIFY = 0x002
IN_ATTRIB = 0x004
IN_CLOSE_WRITE = 0x008
IN_MOVED_FROM = 0x040
IN_MOVED_TO = 0x080
IN_CREATE = 0x100
IN_DELETE = 0x200
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0o4000
MASK = (
    IN_MODIFY
    | IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
)
NAMES = {
    IN_MODIFY: "modify",
    IN_ATTRIB: "attrib",
    IN_CLOSE_WRITE: "close_write",
    IN_MOVED_FROM: "moved_from",
    IN_MOVED_TO: "moved_to",
    IN_CREATE: "create",
    IN_DELETE: "delete",
}
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class WriteAudit:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.fd = _libc.inotify_init1(IN_NONBLOCK)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.watches: dict[int, Path] = {}
        self.events: list[tuple[str, str]] = []
        for dirpath, _dirs, _files in os.walk(self.root):
            self._watch(Path(dirpath))

    def _watch(self, path: Path) -> None:
        wd = _libc.inotify_add_watch(self.fd, os.fsencode(path), MASK)
        if wd >= 0:
            self.watches[wd] = path

    def drain(self) -> None:
        while select.select([self.fd], [], [], 0)[0]:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            offset = 0
            while offset < len(data):
                wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                name = data[offset + 16 : offset + 16 + length].rstrip(b"\0")
                offset += 16 + length
                base = self.watches.get(wd)
                if base is None:
                    continue
                path = base / os.fsdecode(name) if name else base
                for bit, label in NAMES.items():
                    if mask & bit:
                        rel = os.path.relpath(path, self.root)
                        self.events.append((label, rel))
                if mask & IN_CREATE and mask & IN_ISDIR:
                    # Watch new directories and replay their existing children.
                    for dirpath, _dirs, files in os.walk(path):
                        self._watch(Path(dirpath))
                        for f in files:
                            rel = os.path.relpath(Path(dirpath) / f, self.root)
                            self.events.append(("create", rel))

    def close(self) -> list[tuple[str, str]]:
        self.drain()
        os.close(self.fd)
        return self.events

    def paths(self) -> list[str]:
        return sorted({p for _e, p in self.events})
