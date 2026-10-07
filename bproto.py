"""bproto: the wire format shared by bserve and bcurl.  SPEC.md is the
normative version; this module is one implementation of it.

Every frame is a 5-byte header followed by `length` payload bytes:

    +--------+--------+--------+--------+--------+=============+
    |        length (24 bits)  |  type  | flags  |   payload   |
    +--------+--------+--------+--------+--------+=============+

All integers are big-endian.  A receiver that meets a frame type it does
not know reads `length` bytes and throws them away: that rule is what lets
a version 2 add frame types without breaking version 1 peers.
"""

import socket
import struct
from collections import namedtuple

HEADER_LEN = 5
MAX_LENGTH = (1 << 24) - 1     # what 24 bits can say: 16 MiB - 1

# Frame types.  0xF0-0xFF are reserved for "grease": never assigned, so
# a sender may use them to check that the peer really skips unknown frames.
DATA, REQUEST, RESPONSE = 0x00, 0x01, 0x02
TYPE_NAMES = {DATA: "DATA", REQUEST: "REQUEST", RESPONSE: "RESPONSE"}
GREASE = 0xFA

# Flags.  Unknown flag bits are ignored.
END_MESSAGE = 0x01

# Methods (one byte in a REQUEST frame).
GET, HEAD = 0x01, 0x02
METHODS = {GET: "GET", HEAD: "HEAD"}
METHOD_CODES = {name: code for code, name in METHODS.items()}

# The static header table: the ten names bserve and bcurl actually send.
# Index 0 means "literal name follows"; 11-255 are unassigned, and a
# receiver skips a header whose index it does not know.
STATIC_NAMES = (None, "host", "user-agent", "accept", "server", "date",
                "content-type", "content-length", "last-modified", "etag",
                "connection")
NAME_INDEX = {name: i for i, name in enumerate(STATIC_NAMES) if name}

REASONS = {
    200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 500: "Internal Server Error",
}

TOKEN_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~")

Frame = namedtuple("Frame", "type flags payload raw")


class Malformed(Exception):
    """A frame's payload does not parse.  The frame boundary is still
    known, so the connection survives: answer 400 and read the next one."""


class FrameTooLarge(Exception):
    """The peer announced a frame bigger than we are willing to buffer."""

    def __init__(self, ftype, length):
        super().__init__("frame of %d bytes is too large" % length)
        self.type = ftype
        self.length = length


class PeerClosed(Exception):
    """EOF.  `clean` is True when it fell between two frames."""

    def __init__(self, clean):
        super().__init__("peer closed the connection" +
                         ("" if clean else " in the middle of a frame"))
        self.clean = clean


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------

def encode_frame(ftype, flags, payload=b""):
    if len(payload) > MAX_LENGTH:
        raise ValueError("payload of %d bytes does not fit in 24 bits"
                         % len(payload))
    return len(payload).to_bytes(3, "big") + bytes((ftype, flags)) + payload


class FrameReader:
    """Cuts frames off a socket.  Bytes past the current frame stay in
    `buf`: they are the start of the next one (pipelining comes free)."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise PeerClosed(clean=not self.buf)
            self.buf += chunk

    def read_frame(self, max_length=MAX_LENGTH):
        self._fill(HEADER_LEN)
        length = int.from_bytes(self.buf[:3], "big")
        ftype, flags = self.buf[3], self.buf[4]
        if length > max_length:
            raise FrameTooLarge(ftype, length)
        self._fill(HEADER_LEN + length)
        raw = bytes(self.buf[:HEADER_LEN + length])
        del self.buf[:HEADER_LEN + length]
        return Frame(ftype, flags, raw[HEADER_LEN:], raw)


# --------------------------------------------------------------------------
# Payload fields
# --------------------------------------------------------------------------

class Cursor:
    """Reads fields off a payload; running off the end is Malformed."""

    def __init__(self, buf):
        self.buf = buf
        self.pos = 0

    def done(self):
        return self.pos == len(self.buf)

    def take(self, n, what):
        if self.pos + n > len(self.buf):
            raise Malformed("truncated %s" % what)
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self, what):
        return self.take(1, what)[0]

    def u16(self, what):
        return struct.unpack("!H", self.take(2, what))[0]


def encode_headers(headers):
    out = bytearray()
    for name, value in headers:
        name = name.lower()
        raw_value = value.encode("utf-8")
        if len(raw_value) > 0xFFFF:
            raise ValueError("header %r: value longer than 65535 bytes" % name)
        index = NAME_INDEX.get(name)
        if index:
            out.append(index)
        else:
            raw_name = name.encode("ascii")
            if not 0 < len(raw_name) < 256 or not set(raw_name) <= TOKEN_CHARS:
                raise ValueError("bad header name %r" % name)
            out += bytes((0, len(raw_name))) + raw_name
        out += struct.pack("!H", len(raw_value)) + raw_value
    return bytes(out)


def decode_headers(cur):
    """Headers run to the end of the payload: the frame length is the
    count, so there is no separate one to disagree with it."""
    headers = []
    while not cur.done():
        index = cur.u8("header index")
        if index == 0:
            raw_name = cur.take(cur.u8("header name length"), "header name")
            if not raw_name or not set(raw_name) <= TOKEN_CHARS:
                raise Malformed("bad literal header name %r" % raw_name)
            name = raw_name.decode("ascii")
        elif index < len(STATIC_NAMES):
            name = STATIC_NAMES[index]
        else:
            name = None                 # unknown index: skip this header
        raw_value = cur.take(cur.u16("header value length"), "header value")
        if name is None:
            continue
        try:
            headers.append((name, raw_value.decode("utf-8")))
        except UnicodeDecodeError:
            raise Malformed("header %r is not UTF-8" % name)
    return headers


def get_header(headers, name):
    for n, v in headers:
        if n == name:
            return v
    return None


# --------------------------------------------------------------------------
# REQUEST and RESPONSE frames
# --------------------------------------------------------------------------

def encode_request(method, path, headers, end=True):
    raw_path = path.encode("utf-8")
    payload = (bytes((method,)) + struct.pack("!H", len(raw_path)) +
               raw_path + encode_headers(headers))
    return encode_frame(REQUEST, END_MESSAGE if end else 0, payload)


def decode_request(payload):
    """Returns (method code, path, headers).  An unknown method code is
    not malformed - it parses fine - so it is left for the server (405)."""
    cur = Cursor(payload)
    method = cur.u8("method")
    raw_path = cur.take(cur.u16("path length"), "path")
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        raise Malformed("path is not UTF-8")
    if not path.startswith("/") or "\0" in path:
        raise Malformed("path must start with '/' and contain no NUL")
    return method, path, decode_headers(cur)


def encode_response(status, headers, end):
    payload = struct.pack("!H", status) + encode_headers(headers)
    return encode_frame(RESPONSE, END_MESSAGE if end else 0, payload)


def decode_response(payload):
    cur = Cursor(payload)
    status = cur.u16("status")
    if not 100 <= status <= 599:
        raise Malformed("status %d out of range" % status)
    return status, decode_headers(cur)


# --------------------------------------------------------------------------
# Showing bytes to humans (bcurl -v)
# --------------------------------------------------------------------------

def hexdump(data, indent="    "):
    """`hexdump -C` layout: offset, 16 bytes in two groups, ASCII."""
    lines = []
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexes = " ".join("%02x" % b for b in row[:8])
        if len(row) > 8:
            hexes += "  " + " ".join("%02x" % b for b in row[8:])
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append("%s%08x  %-49s |%s|" % (indent, off, hexes, text))
    return "\n".join(lines)


def describe(frame):
    """One header line plus decoded fields, for -v output."""
    name = TYPE_NAMES.get(frame.type, "type 0x%02x" % frame.type)
    flags = "END_MESSAGE" if frame.flags & END_MESSAGE else "-"
    head = "%s  length=%d flags=%s" % (name, len(frame.payload), flags)
    detail = []
    try:
        if frame.type == REQUEST:
            method, path, headers = decode_request(frame.payload)
            detail.append("%s %s" % (METHODS.get(method, "method 0x%02x"
                                                  % method), path))
            detail += ["%s: %s" % h for h in headers]
        elif frame.type == RESPONSE:
            status, headers = decode_response(frame.payload)
            detail.append("%d %s" % (status, REASONS.get(status, "")))
            detail += ["%s: %s" % h for h in headers]
        elif frame.type not in TYPE_NAMES:
            head += "  (unknown type: skipped)"
    except Malformed as e:
        detail.append("malformed: %s" % e)
    return head, detail


def make_listener(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(64)
    return s
