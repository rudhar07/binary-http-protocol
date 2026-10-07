#!/usr/bin/env python3
"""Tests for bproto, bserve and bcurl.  Run:  python3 test_bproto.py

The server tests speak raw frames over real sockets, so they can send
what bcurl never would: unknown types, truncated payloads, bodies.
"""

import io
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

import bproto
import client
import server
from bproto import DATA, END_MESSAGE, GET, HEAD, REQUEST, RESPONSE

HERE = os.path.dirname(os.path.realpath(__file__))
server.LOG = False

ROOT = tempfile.mkdtemp()
BIG = os.urandom(40 * 1024 + 7)            # three DATA frames
with open(os.path.join(ROOT, "index.html"), "w") as f:
    f.write("<h1>home</h1>\n")
with open(os.path.join(ROOT, "big.bin"), "wb") as f:
    f.write(BIG)
open(os.path.join(ROOT, "empty.txt"), "w").close()
os.mkdir(os.path.join(ROOT, "sub"))
with open(os.path.join(ROOT, "sub", "index.html"), "w") as f:
    f.write("sub\n")
with open(os.path.join(os.path.dirname(ROOT), "secret.txt"), "w") as f:
    f.write("outside the root\n")


class CountingListener:
    """Runs bserve's handler on a fresh port and counts accepted
    connections, so tests can prove bcurl used exactly one."""

    def __init__(self):
        self.listener = bproto.make_listener("127.0.0.1", 0)
        self.port = self.listener.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        root = os.path.realpath(ROOT)
        while True:
            sock, _ = self.listener.accept()
            self.accepted += 1
            threading.Thread(target=server.handle_connection,
                             args=(sock, root), daemon=True).start()


SERVER = CountingListener()


class Peer:
    """A raw-frame client: sends bytes, reads whole response messages."""

    def __init__(self, port=None):
        self.sock = socket.create_connection(
            ("127.0.0.1", port or SERVER.port), timeout=3)
        self.reader = bproto.FrameReader(self.sock)

    def send(self, raw):
        self.sock.sendall(raw)

    def get(self, path, method=GET):
        self.send(bproto.encode_request(method, path, [("host", "t")]))
        return self.response()

    def response(self):
        """(status, headers dict, body, number of DATA frames)."""
        frame = self.reader.read_frame()
        assert frame.type == RESPONSE, frame
        status, headers = bproto.decode_response(frame.payload)
        body, frames = b"", 0
        end = frame.flags & END_MESSAGE
        while not end:
            frame = self.reader.read_frame()
            assert frame.type == DATA, frame
            body += frame.payload
            frames += 1
            end = frame.flags & END_MESSAGE
        return status, dict(headers), body, frames

    def closed(self):
        try:
            return self.sock.recv(1) == b""
        except socket.timeout:
            return False

    def close(self):
        self.sock.close()


# --------------------------------------------------------------------------
# The codec
# --------------------------------------------------------------------------

class CodecTests(unittest.TestCase):

    def test_frame_header_layout(self):
        raw = bproto.encode_frame(RESPONSE, END_MESSAGE, b"ab")
        self.assertEqual(raw, b"\x00\x00\x02\x02\x01ab")

    def test_length_limit_is_24_bits(self):
        bproto.encode_frame(DATA, 0, bytes(bproto.MAX_LENGTH))
        with self.assertRaises(ValueError):
            bproto.encode_frame(DATA, 0, bytes(bproto.MAX_LENGTH + 1))

    def test_request_round_trip(self):
        headers = [("host", "h:1"), ("x-trace", "abc"), ("accept", "*/*")]
        raw = bproto.encode_request(GET, "/a/b.txt", headers)
        method, path, back = bproto.decode_request(raw[bproto.HEADER_LEN:])
        self.assertEqual((method, path, back), (GET, "/a/b.txt", headers))

    def test_indexed_names_cost_one_byte(self):
        self.assertEqual(bproto.encode_headers([("host", "h")]),
                         b"\x01\x00\x01h")
        self.assertEqual(bproto.encode_headers([("x-a", "h")]),
                         b"\x00\x03x-a\x00\x01h")

    def test_unknown_header_index_is_skipped(self):
        block = b"\x63\x00\x03zzz" + bproto.encode_headers([("host", "h")])
        self.assertEqual(bproto.decode_headers(bproto.Cursor(block)),
                         [("host", "h")])

    def test_truncation_is_malformed(self):
        good = bproto.encode_request(GET, "/x", [("host", "h")])[5:]
        for cut in range(len(good)):
            if cut == 3 + 2:               # ends right after the path
                continue
            with self.assertRaises(bproto.Malformed, msg="cut=%d" % cut):
                bproto.decode_request(good[:cut])

    def test_path_must_be_absolute(self):
        with self.assertRaises(bproto.Malformed):
            bproto.decode_request(b"\x01\x00\x01x")

    def test_bad_status_is_malformed(self):
        with self.assertRaises(bproto.Malformed):
            bproto.decode_response(struct.pack("!H", 42))

    def test_hexdump_matches_hexdump_c(self):
        self.assertEqual(bproto.hexdump(b"\x00AB", indent=""),
                         "00000000  00 41 42" + " " * 42 + "|.AB|")


# --------------------------------------------------------------------------
# bserve, over sockets
# --------------------------------------------------------------------------

class ServerTests(unittest.TestCase):

    def setUp(self):
        self.peer = Peer()

    def tearDown(self):
        self.peer.close()

    def test_get_file(self):
        status, headers, body, _ = self.peer.get("/index.html")
        self.assertEqual((status, body), (200, b"<h1>home</h1>\n"))
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["content-length"], "14")
        self.assertIn("etag", headers)

    def test_directory_serves_index(self):
        self.assertEqual(self.peer.get("/")[2], b"<h1>home</h1>\n")
        self.assertEqual(self.peer.get("/sub")[2], b"sub\n")

    def test_big_file_is_split_into_data_frames(self):
        status, headers, body, frames = self.peer.get("/big.bin")
        self.assertEqual((status, body, frames), (200, BIG, 3))
        self.assertEqual(headers["content-length"], str(len(BIG)))

    def test_empty_file_has_no_data_frame(self):
        status, _, body, frames = self.peer.get("/empty.txt")
        self.assertEqual((status, body, frames), (200, b"", 0))

    def test_head_has_headers_but_no_body(self):
        status, headers, body, frames = self.peer.get("/big.bin", HEAD)
        self.assertEqual((status, body, frames), (200, b"", 0))
        self.assertEqual(headers["content-length"], str(len(BIG)))

    def test_404_and_connection_stays_open(self):
        self.assertEqual(self.peer.get("/missing")[0], 404)
        self.assertEqual(self.peer.get("/index.html")[0], 200)

    def test_traversal_stays_inside_root(self):
        for path in ("/../secret.txt", "/sub/../../secret.txt"):
            self.assertEqual(self.peer.get(path)[0], 404, path)

    def test_query_string_is_ignored(self):
        self.assertEqual(self.peer.get("/index.html?x=1")[0], 200)

    def test_malformed_payload_is_400_and_connection_survives(self):
        self.peer.send(bproto.encode_frame(REQUEST, END_MESSAGE,
                                           b"\x01\x00\x09/short"))
        self.assertEqual(self.peer.response()[0], 400)
        self.peer.send(bproto.encode_frame(REQUEST, END_MESSAGE,
                                           b"\x01\x00\x03abc"))
        self.assertEqual(self.peer.response()[0], 400)
        self.assertEqual(self.peer.get("/index.html")[0], 200)

    def test_unknown_method_is_405(self):
        self.peer.send(bproto.encode_request(0x09, "/", []))
        self.assertEqual(self.peer.response()[0], 405)

    def test_unknown_frame_types_are_skipped(self):
        junk = (bproto.encode_frame(0xFA, 0, b"grease") +
                bproto.encode_frame(0x33, 0xFF, os.urandom(300)))
        self.peer.send(junk + bproto.encode_request(GET, "/", []) + junk)
        self.assertEqual(self.peer.response()[0], 200)
        self.assertEqual(self.peer.get("/index.html")[0], 200)

    def test_request_body_is_consumed(self):
        self.peer.send(bproto.encode_request(GET, "/index.html", [], end=False)
                       + bproto.encode_frame(DATA, 0, b"x" * 100)
                       + bproto.encode_frame(0xFB, 0, b"skip")
                       + bproto.encode_frame(DATA, END_MESSAGE, b"y"))
        self.assertEqual(self.peer.response()[0], 200)
        self.assertEqual(self.peer.get("/index.html")[0], 200)

    def test_data_without_request_is_400(self):
        self.peer.send(bproto.encode_frame(DATA, END_MESSAGE, b"stray"))
        self.assertEqual(self.peer.response()[0], 400)
        self.assertEqual(self.peer.get("/")[0], 200)

    def test_pipelined_requests_answered_in_order(self):
        self.peer.send(bproto.encode_request(GET, "/missing", []) +
                       bproto.encode_request(GET, "/sub/", []) +
                       bproto.encode_request(GET, "/", []))
        self.assertEqual(self.peer.response()[0], 404)
        self.assertEqual(self.peer.response()[2], b"sub\n")
        self.assertEqual(self.peer.response()[2], b"<h1>home</h1>\n")

    def test_request_split_across_tcp_segments(self):
        raw = bproto.encode_request(GET, "/index.html", [("host", "t")])
        for b in raw:
            self.peer.send(bytes((b,)))
        self.assertEqual(self.peer.response()[0], 200)

    def test_oversized_frame_is_400_then_close(self):
        self.peer.send(b"\xff\xff\xff\x01\x01")
        status, headers, _, _ = self.peer.response()
        self.assertEqual((status, headers["connection"]), (400, "close"))
        self.assertTrue(self.peer.closed())


# --------------------------------------------------------------------------
# bcurl
# --------------------------------------------------------------------------

def run_bcurl(*args):
    out, err = io.BytesIO(), io.BytesIO()
    code = client.main(list(args), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class ClientTests(unittest.TestCase):

    def url(self, path):
        return "localhost:%d%s" % (SERVER.port, path)

    def test_body_to_stdout_exit_0(self):
        code, out, _ = run_bcurl(self.url("/index.html"))
        self.assertEqual((code, out), (0, b"<h1>home</h1>\n"))

    def test_404_exits_nonzero_and_keeps_stdout_clean(self):
        code, out, err = run_bcurl(self.url("/missing"))
        self.assertEqual((code, out), (4, b""))
        self.assertIn(b"404", err)

    def test_many_targets_one_connection(self):
        before = SERVER.accepted
        code, out, _ = run_bcurl("--grease", self.url("/sub/"), "/",
                                 self.url("/big.bin"))
        self.assertEqual(code, 0)
        self.assertEqual(out, b"sub\n<h1>home</h1>\n" + BIG)
        self.assertEqual(SERVER.accepted - before, 1)

    def test_other_host_is_refused_not_connected(self):
        before = SERVER.accepted
        code, _, _ = run_bcurl(self.url("/"), "localhost:1/x")
        self.assertEqual((code, SERVER.accepted - before), (1, 0))

    def test_client_skips_unknown_frames_from_server(self):
        listener = bproto.make_listener("127.0.0.1", 0)

        def fake_server():
            sock, _ = listener.accept()
            bproto.FrameReader(sock).read_frame()
            sock.sendall(bproto.encode_frame(0xFC, 0, b"v2 stuff") +
                         bproto.encode_response(200, [], end=False) +
                         bproto.encode_frame(0xFD, 0, b"") +
                         bproto.encode_frame(DATA, END_MESSAGE, b"ok"))
            sock.close()

        threading.Thread(target=fake_server, daemon=True).start()
        port = listener.getsockname()[1]
        code, out, _ = run_bcurl("localhost:%d/" % port)
        listener.close()
        self.assertEqual((code, out), (0, b"ok"))

    def test_verbose_hexdumps_every_frame(self):
        proc = subprocess.run(
            [os.path.join(HERE, "bcurl"), "-v", self.url("/index.html")],
            capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        err = proc.stderr.decode()
        self.assertIn("> REQUEST", err)
        self.assertIn("< RESPONSE", err)
        self.assertIn("< DATA", err)
        self.assertIn("00000000  00 00", err)

    def test_launcher_exit_code_on_404(self):
        proc = subprocess.run([os.path.join(HERE, "bcurl"),
                               self.url("/nope")], capture_output=True,
                              timeout=10)
        self.assertEqual(proc.returncode, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
