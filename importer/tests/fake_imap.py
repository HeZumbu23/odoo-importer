"""Minimal in-memory IMAP4rev1 server for tests (LOGIN, LIST, LSUB, CREATE, SUBSCRIBE, SELECT/EXAMINE,
UID SEARCH, UID FETCH, APPEND, NOOP, LOGOUT). Only what the importer and the seed code use."""
from __future__ import annotations

import re
import socketserver
import threading
from datetime import datetime


class Mailbox:
    def __init__(self):
        self.uidvalidity = 1
        self.next_uid = 1
        self.msgs: list[tuple[int, bytes, tuple]] = []  # (uid, raw, flags)


class Store:
    def __init__(self, users: dict[str, str]):
        self.users = users
        self.boxes: dict[str, dict[str, Mailbox]] = {u: {"INBOX": Mailbox()} for u in users}
        self.lock = threading.Lock()

    def add(self, user: str, folder: str, raw: bytes, flags=()) -> int:
        with self.lock:
            box = self.boxes[user].setdefault(folder, Mailbox())
            uid = box.next_uid
            box.next_uid += 1
            box.msgs.append((uid, raw, tuple(flags)))
            return uid


def _tokens(line: str) -> list[str]:
    out, i = [], 0
    while i < len(line):
        c = line[i]
        if c == " ":
            i += 1
        elif c == '"':
            j = i + 1
            buf = ""
            while line[j] != '"':
                if line[j] == "\\":
                    j += 1
                buf += line[j]
                j += 1
            out.append(buf)
            i = j + 1
        elif c == "(":
            depth, j = 1, i + 1
            while depth:
                depth += {"(": 1, ")": -1}.get(line[j], 0)
                j += 1
            out.append(line[i:j])
            i = j
        else:
            j = i
            while j < len(line) and line[j] != " ":
                j += 1
            out.append(line[i:j])
            i = j
    return out


def _uid_set(spec: str, uids: list[int]) -> list[int]:
    hi = max(uids) if uids else 0
    chosen: set[int] = set()
    for part in spec.split(","):
        if ":" in part:
            a, b = part.split(":")
            lo = hi if a == "*" else int(a)
            up = hi if b == "*" else int(b)
            lo, up = min(lo, up), max(lo, up)
            chosen.update(u for u in uids if lo <= u <= up)
        else:
            n = hi if part == "*" else int(part)
            if n in uids:
                chosen.add(n)
    return sorted(chosen)


class Handler(socketserver.StreamRequestHandler):
    store: Store

    def send(self, text: str | bytes):
        data = text if isinstance(text, bytes) else text.encode()
        self.wfile.write(data + b"\r\n")
        self.wfile.flush()

    def handle(self):
        user = None
        selected: tuple[str, Mailbox] | None = None
        self.send("* OK [CAPABILITY IMAP4rev1 UIDPLUS] fake imap ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            tag, _, rest = line.partition(" ")
            toks = _tokens(rest)
            cmd = toks[0].upper() if toks else ""
            args = toks[1:]
            try:
                if cmd == "CAPABILITY":
                    self.send("* CAPABILITY IMAP4rev1 UIDPLUS")
                    self.send(f"{tag} OK done")
                elif cmd == "NOOP":
                    self.send(f"{tag} OK done")
                elif cmd == "LOGIN":
                    if self.store.users.get(args[0]) == args[1]:
                        user = args[0]
                        self.send(f"{tag} OK [CAPABILITY IMAP4rev1 UIDPLUS] logged in")
                    else:
                        self.send(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
                elif cmd == "LOGOUT":
                    self.send("* BYE bye")
                    self.send(f"{tag} OK done")
                    return
                elif user is None:
                    self.send(f"{tag} NO login first")
                elif cmd in ("LIST", "LSUB"):
                    pattern = args[1] if len(args) > 1 else "*"
                    rx = re.compile("^" + re.escape(pattern).replace(r"\*", ".*").replace(r"%", "[^/]*") + "$")
                    for name in sorted(self.store.boxes[user]):
                        if rx.match(name):
                            self.send(f'* {cmd} (\\HasNoChildren) "/" "{name}"')
                    self.send(f"{tag} OK done")
                elif cmd == "CREATE":
                    self.store.boxes[user].setdefault(args[0], Mailbox())
                    self.send(f"{tag} OK created")
                elif cmd in ("SUBSCRIBE", "UNSUBSCRIBE"):
                    self.send(f"{tag} OK done")
                elif cmd in ("SELECT", "EXAMINE"):
                    box = self.store.boxes[user].get(args[0])
                    if box is None:
                        self.send(f"{tag} NO no such mailbox")
                        continue
                    selected = (args[0], box)
                    self.send("* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)")
                    self.send(f"* {len(box.msgs)} EXISTS")
                    self.send("* 0 RECENT")
                    self.send(f"* OK [UIDVALIDITY {box.uidvalidity}] ok")
                    self.send(f"* OK [UIDNEXT {box.next_uid}] ok")
                    mode = "READ-ONLY" if cmd == "EXAMINE" else "READ-WRITE"
                    self.send(f"{tag} OK [{mode}] done")
                elif cmd == "APPEND":
                    folder = args[0]
                    flags = ()
                    for t in args[1:]:
                        if t.startswith("("):
                            flags = tuple(t.strip("()").split())
                    size = int(re.search(r"\{(\d+)\+?\}$", rest).group(1))
                    self.send("+ go ahead")
                    data = self.rfile.read(size)
                    self.rfile.readline()
                    if folder not in self.store.boxes[user]:
                        self.send(f"{tag} NO [TRYCREATE] no such mailbox")
                        continue
                    uid = self.store.add(user, folder, data, flags)
                    box = self.store.boxes[user][folder]
                    self.send(f"{tag} OK [APPENDUID {box.uidvalidity} {uid}] done")
                elif cmd == "UID" and args and args[0].upper() == "SEARCH":
                    box = selected[1]
                    crit = [a for a in args[1:]]
                    uids = [m[0] for m in box.msgs]
                    i = 0
                    while i < len(crit):
                        c = crit[i].upper()
                        if c == "UID":
                            uids = [u for u in uids if u in _uid_set(crit[i + 1], uids)]
                            i += 2
                        elif c == "SINCE":
                            since = datetime.strptime(crit[i + 1], "%d-%b-%Y")
                            uids = [u for u in uids if _msg_date(box, u) >= since]
                            i += 2
                        elif c == "HEADER":
                            name, val = crit[i + 1].lower(), crit[i + 2].lower()
                            uids = [u for u in uids if f"{name}: {val}".encode() in _msg(box, u).lower()]
                            i += 3
                        else:
                            i += 1
                    self.send("* SEARCH " + " ".join(map(str, uids)) if uids else "* SEARCH")
                    self.send(f"{tag} OK done")
                elif cmd == "UID" and args and args[0].upper() == "FETCH":
                    box = selected[1]
                    seq = {m[0]: i + 1 for i, m in enumerate(box.msgs)}
                    items = args[2].upper()
                    for uid in _uid_set(args[1], [m[0] for m in box.msgs]):
                        raw = _msg(box, uid)
                        parts = [f"UID {uid}"]
                        if "RFC822.SIZE" in items:
                            parts.append(f"RFC822.SIZE {len(raw)}")
                        if "FLAGS" in items:
                            parts.append("FLAGS ()")
                        if "BODY" in items:
                            head = f"* {seq[uid]} FETCH ({' '.join(parts)} BODY[] {{{len(raw)}}}".encode()
                            self.wfile.write(head + b"\r\n" + raw + b")\r\n")
                            self.wfile.flush()
                        else:
                            self.send(f"* {seq[uid]} FETCH ({' '.join(parts)})")
                    self.send(f"{tag} OK done")
                else:
                    self.send(f"{tag} BAD unsupported: {cmd}")
            except Exception as exc:  # noqa: BLE001
                self.send(f"{tag} BAD {type(exc).__name__}: {exc}")


def _msg(box: Mailbox, uid: int) -> bytes:
    return next(m[1] for m in box.msgs if m[0] == uid)


def _msg_date(box: Mailbox, uid: int) -> datetime:
    import email
    import email.utils

    d = email.utils.parsedate_to_datetime(str(email.message_from_bytes(_msg(box, uid)).get("Date")))
    return d.replace(tzinfo=None)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(users: dict[str, str], host: str = "127.0.0.1", port: int = 0) -> tuple[_Server, Store]:
    store = Store(users)
    handler = type("H", (Handler,), {"store": store})
    server = _Server((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, store


if __name__ == "__main__":  # python tests/fake_imap.py 1143 archiv:archiv,vertrieb:vertrieb
    import sys

    srv, _ = start(dict(p.split(":") for p in sys.argv[2].split(",")), "0.0.0.0", int(sys.argv[1]))
    print("fake imap on", srv.server_address, flush=True)
    threading.Event().wait()
