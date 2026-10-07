"""bcurl: fetch paths over the binary protocol, on exactly one connection.

    ./bcurl [-v] [-I] [-H 'name: value']... [--grease] host:port/path [more...]

Extra targets are either `/path` or the same `host:port/path`; they are
requested one after another on the socket that is already open.  A
target on another host:port is refused rather than given a second
connection.

Bodies of 2xx responses go to stdout; error bodies go to stderr.
-v writes every frame, sent (>) and received (<), to stderr as a hexdump.
Exit status: 0 if every response was 2xx/3xx, 4 if the worst was 4xx,
5 if the worst was 5xx, 1 for usage, connection or protocol errors.
"""

import argparse
import socket
import sys

import bproto
from bproto import (DATA, END_MESSAGE, RESPONSE, TYPE_NAMES, FrameReader,
                    Malformed, PeerClosed)

USER_AGENT = "bcurl/1.0"
DEFAULT_PORT = 9000
TIMEOUT = 10.0


class ClientError(Exception):
    pass


def parse_target(text):
    """'host:port/path' -> (host, port, path).  A leading 'bhttp://' is
    allowed; a missing port means 9000, a missing path means '/'."""
    if text.startswith("bhttp://"):
        text = text[len("bhttp://"):]
    hostport, slash, path = text.partition("/")
    path = slash + path if slash else "/"
    host, colon, port = hostport.partition(":")
    if not host:
        raise ClientError("no host in %r" % text)
    try:
        port = int(port) if colon else DEFAULT_PORT
    except ValueError:
        raise ClientError("bad port in %r" % text)
    return host, port, path


class Connection:
    def __init__(self, host, port, verbose, out=sys.stderr):
        self.verbose = verbose
        self.out = out
        try:
            self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        except OSError as e:
            raise ClientError("cannot connect to %s:%d: %s" % (host, port, e))
        self.reader = FrameReader(self.sock)
        self.authority = "%s:%d" % (host, port)
        self.show("* connected to %s" % self.authority)

    def show(self, line):
        if self.verbose:
            print(line, file=self.out, flush=True)

    def show_frame(self, arrow, raw):
        if not self.verbose:
            return
        frame = bproto.Frame(raw[3], raw[4], raw[bproto.HEADER_LEN:], raw)
        head, detail = bproto.describe(frame)
        self.show("%s %s" % (arrow, head))
        for line in detail:
            self.show("    %s" % line)
        self.show(bproto.hexdump(raw))

    def send(self, raw):
        self.show_frame(">", raw)
        self.sock.sendall(raw)

    def recv(self):
        frame = self.reader.read_frame()
        self.show_frame("<", frame.raw)
        return frame

    def fetch(self, method, path, extra_headers, grease, body_out, err_out):
        """One request/response exchange.  Returns the status."""
        if grease:
            self.send(bproto.encode_frame(bproto.GREASE, 0, b"skip me"))
        headers = [("host", self.authority), ("user-agent", USER_AGENT),
                   ("accept", "*/*")] + extra_headers
        self.send(bproto.encode_request(method, path, headers))

        frame = self.recv()
        while frame.type != RESPONSE:
            if frame.type in TYPE_NAMES:
                raise ClientError("got %s before the RESPONSE"
                                  % TYPE_NAMES[frame.type])
            frame = self.recv()                     # unknown type: skip
        try:
            status, _headers = bproto.decode_response(frame.payload)
        except Malformed as e:
            raise ClientError("malformed RESPONSE: %s" % e)

        out = body_out if status < 400 else err_out
        if status >= 400:
            err_out.write(("bcurl: %s %d %s\n" % (
                path, status, bproto.REASONS.get(status, ""))).encode())
        end = frame.flags & END_MESSAGE
        while not end:
            frame = self.recv()
            if frame.type == DATA:
                out.write(frame.payload)
                end = frame.flags & END_MESSAGE
            elif frame.type in TYPE_NAMES:
                raise ClientError("got %s inside a response body"
                                  % TYPE_NAMES[frame.type])
        out.flush()
        return status

    def close(self):
        self.sock.close()
        self.show("* closed %s" % self.authority)


def build_parser():
    p = argparse.ArgumentParser(
        prog="bcurl", description="Fetch paths over the binary protocol "
                                  "(see SPEC.md), on one connection.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="hexdump every frame to stderr")
    p.add_argument("-I", "--head", action="store_true",
                   help="send HEAD instead of GET")
    p.add_argument("-H", "--header", action="append", default=[],
                   metavar="'NAME: VALUE'", help="add a request header")
    p.add_argument("--grease", action="store_true",
                   help="send an unknown frame type before each request")
    p.add_argument("targets", nargs="+", metavar="host:port/path")
    return p


def main(argv, stdout=None, stderr=None):
    stdout = stdout or sys.stdout.buffer
    stderr = stderr or sys.stderr.buffer
    args = build_parser().parse_args(argv)
    try:
        host, port, first = parse_target(args.targets[0])
        paths = [first]
        for t in args.targets[1:]:
            if t.startswith("/"):
                paths.append(t)
                continue
            h, p, path = parse_target(t)
            if (h, p) != (host, port):
                raise ClientError("%s is on another host:port; bcurl never "
                                  "opens a second connection" % t)
            paths.append(path)
        extra = []
        for h in args.header:
            name, colon, value = h.partition(":")
            if not colon or not name.strip():
                raise ClientError("bad header %r, want 'name: value'" % h)
            extra.append((name.strip().lower(), value.strip()))
        bproto.encode_headers(extra)                # validate names early
    except (ClientError, ValueError) as e:
        print("bcurl: %s" % e, file=sys.stderr)
        return 1

    method = bproto.HEAD if args.head else bproto.GET
    worst = 0
    conn = None
    try:
        conn = Connection(host, port, args.verbose)
        for path in paths:
            worst = max(worst, conn.fetch(method, path, extra, args.grease,
                                          stdout, stderr))
    except PeerClosed as e:
        print("bcurl: %s" % e, file=sys.stderr)
        return 1
    except (ClientError, OSError) as e:
        print("bcurl: %s" % e, file=sys.stderr)
        return 1
    finally:
        if conn:
            conn.close()
    if worst >= 500:
        return 5
    if worst >= 400:
        return 4
    return 0
