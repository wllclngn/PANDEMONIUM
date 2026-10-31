# records.py -- the guest's results channel (ttyS1), parsed on the host.
#
#   @@KV <key>=<value>\n
#   @@FILE <name> <nbytes> <flag>\n<nbytes raw bytes>\n      flag = full | tail
#   @@END\n
#
# A FILE payload is consumed by its length, never scanned, so binary content
# carrying "@@" or newlines cannot be mistaken for a header. Bytes between
# records that are not a header (kernel noise before the init made the port
# raw) are skipped up to the next one. A stream that ends inside a header or a
# payload sets truncated and keeps every record that arrived whole before it.

import re
from dataclasses import dataclass, field
from pathlib import Path

from rig.common import slug

KV, FILE, END = b"@@KV ", b"@@FILE ", b"@@END\n"
TAGS = (KV, FILE, END)
FLAGS = ("full", "tail")
SIZE = re.compile(r"[0-9]+")


@dataclass
class Records:
    kv: dict[str, str] = field(default_factory=dict)
    kv_order: list[tuple[str, str]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    file_flags: dict[str, str] = field(default_factory=dict)
    complete: bool = False
    truncated: bool = False


def _next_header(data: bytes, i: int) -> tuple[int, bytes | None]:
    """The first header at or after i, as (offset, tag). A tag of None means
    no header follows: offset is len(data) when the rest is noise, or where a
    tag begins that the stream cut short."""
    n = len(data)
    while True:
        j = data.find(b"@@", i)
        if j < 0:
            return (n - 1, None) if n > i and data.endswith(b"@") else (n, None)
        head = data[j:j + len(FILE)]
        for tag in TAGS:
            if head.startswith(tag):
                return j, tag
        if j + len(head) == n and any(t.startswith(head) for t in TAGS):
            return j, None
        i = j + 1


def parse(data: bytes) -> Records:
    rec = Records()
    i, n = 0, len(data)
    while i < n:
        j, tag = _next_header(data, i)
        if tag is None:
            rec.truncated = j < n
            break
        if tag == END:
            rec.complete = True
            break
        eol = data.find(b"\n", j)
        if eol < 0:
            rec.truncated = True
            break
        line = data[j + len(tag):eol].rstrip(b"\r").decode(errors="replace")
        i = eol + 1
        if tag == KV:
            key, sep, value = line.partition("=")
            if key and sep:
                rec.kv[key] = value
                rec.kv_order.append((key, value))
            continue
        parts = line.rsplit(" ", 2)
        if len(parts) != 3 or not parts[0] or not SIZE.fullmatch(parts[1]) \
                or parts[2] not in FLAGS:
            continue
        name, size, flag = parts[0], int(parts[1]), parts[2]
        if i + size > n:
            rec.truncated = True
            break
        rec.files[name] = data[i:i + size]
        rec.file_flags[name] = flag
        i += size
        if i == n:
            # The payload is whole; only its closing newline never arrived.
            rec.truncated = True
        elif data[i] == 0x0A:
            i += 1
    return rec


def _safe_name(name: str) -> str:
    # Names arrive from the guest; nothing may step outside files/.
    s = slug(name)
    return s if s not in (".", "..") else "x"


def extract(rec: Records, dest: Path) -> None:
    """Every file record as dest/files/<name>."""
    out = dest / "files"
    out.mkdir(parents=True, exist_ok=True)
    for name, body in rec.files.items():
        (out / _safe_name(name)).write_bytes(body)
