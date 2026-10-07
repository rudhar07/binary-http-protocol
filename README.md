# binary-http-protocol: HTTP, in binary

Course project for Network Architecture. The project specifies a binary framing for HTTP, called **BHTTP/1**, and provides both ends of it:

- **`bserve`**: a file server.
- **`bcurl`**: a client that never opens a second connection.

Everything uses the Python 3 standard library only.

| Hand-in | File |
|---------|------|
| 1. The spec | [SPEC.md](SPEC.md) |
| 2. The program | [bserve](bserve), [bcurl](bcurl), [bproto.py](bproto.py) |
| 3. Annotated hexdump of one request and response | [HEXDUMP.md](HEXDUMP.md) |

## Run it

```
./bserve ./www 9000                      # serve ./www on 127.0.0.1:9000
./bcurl localhost:9000/index.html        # body to stdout
./bcurl -v localhost:9000/hello.txt      # hexdump every frame to stderr
./bcurl localhost:9000/ /hello.txt /docs/   # three requests, one connection
./bcurl -I localhost:9000/hello.txt      # HEAD
./bcurl --grease localhost:9000/         # send an unknown frame type first
python3 test_bproto.py                   # 32 tests, over real sockets
```

To listen on every interface (for a partner on another machine), run `./bserve ./www 9000 0.0.0.0`.

`bcurl` exits with:

| Code | Meaning |
|------|---------|
| 0 | every response was 2xx/3xx |
| 4 | the worst response was 4xx |
| 5 | the worst response was 5xx |
| 1 | usage, connection or protocol error |

Error bodies go to stderr, so `./bcurl host/x > out` never writes a 404 page into `out`.

## The protocol in one screen

```
frame   = length(24) type(8) flags(8) payload(length)
REQUEST = method(8) path_len(16) path header*
RESPONSE= status(16) header*
DATA    = body bytes            flags: 0x01 END_MESSAGE
header  = index(8) [name_len(8) name if index=0] value_len(16) value
```

- **Header names:** ten common header names are one byte each (static table indexes 1–10); any other name is sent as a length-prefixed literal.
- **Bodies:** a body is split into DATA frames of up to 16 KiB each, so the 24-bit length never limits file size.
- **Unknown frame types:** a receiver skips them. **Unknown header indexes:** the receiver skips that header. That is the room left for a version 2.
- **Malformed payloads:** they get a 400, and the connection stays open, because the frame length still marks where the next frame starts.

[SPEC.md §6](SPEC.md#6-why-these-widths) explains why the header is 24/8/8 and why HTTP/2's 31-bit stream id was dropped.

## Files

| File | What it does |
|------|--------------|
| `bproto.py` | Frame and header codec, `FrameReader`, the hexdump used by `-v`. |
| `server.py` | `bserve`: one thread per connection, path → file under the root, 400/403/404/405, idle timeout. |
| `client.py` | `bcurl`: URL parsing, one connection for every target, `-v`/`-I`/`-H`/`--grease`. |
| `bserve`, `bcurl` | Executable launchers for the two programs. |
| `test_bproto.py` | Codec tests, raw-frame server tests (pipelining, unknown frames, request bodies, traversal, oversize frames), client tests (one-connection count, exit codes, skipping unknown frames from a server). |
| `www/` | Sample files to serve. |
