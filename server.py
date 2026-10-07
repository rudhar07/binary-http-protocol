"""bserve: serve files from a directory over the binary protocol.

    ./bserve <root> <port> [host]          # host defaults to 127.0.0.1

One thread per connection.  Each connection loops: read one request
message (a REQUEST frame, plus DATA frames if it has a body), answer it,
and go back for the next one on the same socket.  Unknown frame types are
skipped wherever they appear.  A malformed payload gets a 400 and the
connection stays open, because the frame length still says where the next
frame starts.  Only a frame we refuse to read at all (too large) ends it.
"""

import mimetypes
import os
import socket
import sys
import threading
from email.utils import formatdate

import bproto
from bproto import (DATA, END_MESSAGE, REQUEST, TYPE_NAMES,
                    FrameReader, FrameTooLarge, Malformed, PeerClosed)

SERVER_NAME = "bserve/1.0"
IDLE_TIMEOUT = 30.0        # seconds a connection may sit silent
MAX_REQUEST_FRAME = 64 * 1024
CHUNK = 16 * 1024          # DATA frame size for file bodies
LOG = True


class ProtocolError(Exception):
    """A frame that parses but is not allowed here (answered with 400)."""


def log(peer, line):
    if LOG:
        print("%s:%d %s" % (peer[0], peer[1], line), file=sys.stderr,
              flush=True)


# --------------------------------------------------------------------------
# Reading one request message
# --------------------------------------------------------------------------

def read_request(reader):
    """Returns the REQUEST frame of the next complete message.  Any body
    (DATA frames up to END_MESSAGE) is read and dropped: GET and HEAD
    have no use for one, but it must be consumed to find the next request.
    """
    while True:
        frame = reader.read_frame(MAX_REQUEST_FRAME)
        if frame.type == REQUEST:
            break
        if frame.type in TYPE_NAMES:
            raise ProtocolError("%s frame where a REQUEST was expected"
                                % TYPE_NAMES[frame.type])
        # unknown type: skipped

    while not frame.flags & END_MESSAGE:
        body = reader.read_frame(MAX_REQUEST_FRAME)
        if body.type == DATA:
            if body.flags & END_MESSAGE:
                break
        elif body.type in TYPE_NAMES:
            raise ProtocolError("%s frame inside a request body"
                                % TYPE_NAMES[body.type])
    return frame


# --------------------------------------------------------------------------
# Mapping a path to a file
# --------------------------------------------------------------------------

def resolve(root, path):
    """The file under `root` that `path` names, or None.  Directories
    serve their index.html.  realpath() collapses '..' and symlinks, so
    anything that ends up outside the root is simply not there."""
    path = path.split("?", 1)[0]
    target = os.path.realpath(os.path.join(root, path.lstrip("/")))
    if target != root and not target.startswith(root + os.sep):
        return None
    if os.path.isdir(target):
        target = os.path.join(target, "index.html")
    return target if os.path.isfile(target) else None


def base_headers():
    return [("server", SERVER_NAME), ("date", formatdate(usegmt=True))]


def send_error(sock, status, message, close=False):
    body = ("%d %s: %s\n" % (status, bproto.REASONS[status],
                             message)).encode("utf-8")
    headers = base_headers() + [("content-type", "text/plain; charset=utf-8"),
                                ("content-length", str(len(body)))]
    if close:
        headers.append(("connection", "close"))
    sock.sendall(bproto.encode_response(status, headers, end=False) +
                 bproto.encode_frame(DATA, END_MESSAGE, body))


def send_file(sock, method, target):
    with open(target, "rb") as f:
        st = os.fstat(f.fileno())
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        headers = base_headers() + [
            ("content-type", ctype),
            ("content-length", str(st.st_size)),
            ("last-modified", formatdate(st.st_mtime, usegmt=True)),
            ("etag", '"%x-%x"' % (st.st_size, st.st_mtime_ns)),
        ]
        no_body = method == bproto.HEAD or st.st_size == 0
        sock.sendall(bproto.encode_response(200, headers, end=no_body))
        if no_body:
            return
        # Read one chunk ahead so the last DATA frame can carry END_MESSAGE.
        chunk = f.read(CHUNK)
        while chunk:
            following = f.read(CHUNK)
            sock.sendall(bproto.encode_frame(
                DATA, 0 if following else END_MESSAGE, chunk))
            chunk = following


def answer(sock, root, peer, frame):
    try:
        method, path, _headers = bproto.decode_request(frame.payload)
    except Malformed as e:
        log(peer, "- 400 %s" % e)
        send_error(sock, 400, str(e))
        return

    name = bproto.METHODS.get(method, "0x%02x" % method)
    if method not in bproto.METHODS:
        log(peer, "%s %s 405" % (name, path))
        send_error(sock, 405, "method %s; only GET and HEAD" % name)
        return

    target = resolve(root, path)
    if target is None:
        log(peer, "%s %s 404" % (name, path))
        send_error(sock, 404, path)
        return
    try:
        send_file(sock, method, target)
        log(peer, "%s %s 200" % (name, path))
    except PermissionError:
        log(peer, "%s %s 403" % (name, path))
        send_error(sock, 403, path)


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

def handle_connection(sock, root):
    try:
        peer = sock.getpeername()
    except OSError:
        sock.close()
        return
    sock.settimeout(IDLE_TIMEOUT)
    reader = FrameReader(sock)
    try:
        while True:
            try:
                frame = read_request(reader)
            except ProtocolError as e:
                log(peer, "- 400 %s" % e)
                send_error(sock, 400, str(e))
                continue
            except FrameTooLarge as e:
                log(peer, "- 400 %s, closing" % e)
                send_error(sock, 400, "%s (limit %d)"
                           % (e, MAX_REQUEST_FRAME), close=True)
                return
            answer(sock, root, peer, frame)
    except PeerClosed as e:
        if not e.clean:
            log(peer, "closed mid-frame")
    except socket.timeout:
        log(peer, "idle for %gs, closing" % IDLE_TIMEOUT)
    except OSError as e:
        log(peer, "socket error: %s" % e)
    finally:
        sock.close()


def serve_forever(listener, root):
    root = os.path.realpath(root)
    while True:
        sock, _ = listener.accept()
        threading.Thread(target=handle_connection, args=(sock, root),
                         daemon=True).start()


def main(argv):
    if len(argv) not in (2, 3):
        print("usage: bserve <root> <port> [host]", file=sys.stderr)
        return 2
    root, port = argv[0], argv[1]
    host = argv[2] if len(argv) == 3 else "127.0.0.1"
    if not os.path.isdir(root):
        print("bserve: %s is not a directory" % root, file=sys.stderr)
        return 2
    try:
        listener = bproto.make_listener(host, int(port))
    except (ValueError, OSError) as e:
        print("bserve: cannot listen on %s:%s: %s" % (host, port, e),
              file=sys.stderr)
        return 2
    print("bserve: serving %s on %s:%s" % (os.path.realpath(root), host, port),
          file=sys.stderr, flush=True)
    try:
        serve_forever(listener, root)
    except KeyboardInterrupt:
        return 0
