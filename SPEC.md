# BHTTP/1: HTTP semantics in binary frames

Status: version 1. The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Model

A client opens **one TCP connection** and sends request messages on it, one after another. The server answers each request with one response message, **in the order the requests arrived**. Neither side closes the connection after a response. A client MAY pipeline, which means sending the next request before the previous response arrives. Either side MAY close the connection when it is idle.

A **message** is a REQUEST or RESPONSE frame followed by zero or more DATA frames. The message ends at the first frame that carries the `END_MESSAGE` flag. Frames of different messages never interleave.

All integers are unsigned and big-endian (network byte order). All text is UTF-8.

## 2. Frame header (5 bytes)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-----------------------------------------------+---------------+
|                 Length (24)                   |   Type (8)    |
+---------------+-----------------------------------------------+
|   Flags (8)   |   Payload (Length bytes) ...
+---------------+-----------------------------------------------+
```

| Field   | Bits | Meaning |
|---------|------|---------|
| Length  | 24   | Payload length in bytes, not counting these 5. Maximum 16,777,215. |
| Type    | 8    | Frame type (section 3). |
| Flags   | 8    | `0x01 END_MESSAGE`: last frame of this message. All other bits are reserved: senders MUST set them to 0, and receivers MUST ignore them. |

**Unknown frame types.** A receiver that reads a Type it does not implement MUST read and discard exactly `Length` payload bytes, and MUST then continue as if the frame had never been sent. This applies everywhere, including between a REQUEST and its DATA frames. It MUST NOT treat the frame as an error. Types `0xF0`–`0xFF` are reserved and will never be assigned. Senders MAY send them ("grease", as in RFC 8701) to check that peers really skip unknown types.

## 3. Frame types

| Type | Name     | Sent by | Payload |
|------|----------|---------|---------|
| 0x00 | DATA     | both    | Body bytes, opaque. A body MAY be split across any number of DATA frames. |
| 0x01 | REQUEST  | client  | `method (1) · path_len (2) · path (path_len) · header block` |
| 0x02 | RESPONSE | server  | `status (2) · header block` |

**REQUEST.**
- `method`: `0x01` GET or `0x02` HEAD. Other values are reserved.
- `path` MUST start with `/` and MUST NOT contain NUL. It is sent as raw UTF-8, with no percent-encoding. Anything after the first `?` is a query, which a file server ignores.
- If the request has no body, the REQUEST frame carries `END_MESSAGE`.

**RESPONSE.**
- `status` is an HTTP status code from 100 to 599. No reason phrase is sent; HTTP/2 dropped it too.
- The body follows in DATA frames, and the last of them carries `END_MESSAGE`.
- If there is no body (HEAD, or an empty file), the RESPONSE frame itself carries `END_MESSAGE`.

## 4. Header block

The header block fills the rest of the payload after the fixed fields. There is no header count: the frame Length already says where the block ends, so a second count could only disagree with it. Each header is:

```
index (1) · [ if index = 0:  name_len (1) · name (name_len) ] · value_len (2) · value (value_len)
```

- **Index 1–10** names a header from the static table below. This is HPACK's first mechanism, a static table, without the dynamic part.
- **Index 0** is a literal name: 1–255 bytes of lowercase ASCII token characters (RFC 9110 §5.6.2). Uppercase is malformed.
- **Index 11–255** is unassigned. A receiver MUST skip the header, which it can do because the value is still length-prefixed. This gives a version 2 room to add names, in the same way unknown types give it room to add frames.
- **Values** are always length-prefixed (HPACK's second mechanism, string literals), up to 65,535 bytes of UTF-8, with no Huffman coding. Names may repeat.

| # | name           | # | name           |
|---|----------------|---|----------------|
| 1 | host           | 6 | content-type   |
| 2 | user-agent     | 7 | content-length |
| 3 | accept         | 8 | last-modified  |
| 4 | server         | 9 | etag           |
| 5 | date           | 10 | connection    |

A client SHOULD send `host`. A server SHOULD send `content-length` for bodies so that clients can show progress. DATA framing, not `content-length`, decides where the body ends.

## 5. Errors

| Condition | Response | Connection |
|-----------|----------|------------|
| Payload does not parse: truncated field, path without `/`, invalid UTF-8, status out of range | 400 | stays open |
| Known frame type in the wrong place (DATA with no open request, RESPONSE sent to a server) | 400 | stays open |
| Unknown method code | 405 | stays open |
| Path not found, or resolves outside the server root | 404 | stays open |
| Frame Length above the receiver's limit (bserve: 64 KiB for client frames) | 400 + `connection: close` | closed |
| EOF in the middle of a frame | none | closed |

A malformed *payload* never forces a close, because the Length field still says where the next frame starts. This is the main gain over HTTP/1.1, where a parse error means the receiver no longer knows where the next message begins. Only a frame the receiver refuses to read, or a truncated one, ends the connection.

A server MUST NOT serve a file outside its root. Paths that resolve outside it, via `..` or symlinks, get 404, which is also what a missing file gets.

## 6. Why these widths

HTTP/2 uses a 9-byte header split **24 / 8 / 8 / 31** (plus 1 reserved bit):

- **24-bit length:** frames are meant to be small enough to interleave. The default maximum is 16 KiB, and SETTINGS can raise it to 2²⁴−1, so a fourth byte would never be used.
- **8-bit type:** 256 types, mostly kept for extensions.
- **8 flag bits:** each frame type defines its own.
- **31-bit stream id:** every frame says which of many concurrent streams it belongs to. That is what multiplexing needs. The spare bit was left reserved, which also keeps the id positive in languages with only signed 32-bit integers.

BHTTP/1 keeps the first three fields and drops the fourth:

- **24-bit length, kept.** It caps what a receiver must buffer for one frame at 16 MiB. A file of any size is still possible, because the body is a sequence of DATA frames (bserve sends 16 KiB each). A 32-bit length would invite 4 GiB frames that a receiver could neither buffer nor skip quickly.
- **8-bit type, kept.** Version 1 uses three types. The other 253 are where version 2 lives, and the skip rule in section 2 is what makes adding them safe.
- **8-bit flags, kept.** Version 1 needs one bit (`END_MESSAGE`), and the other seven cost nothing.
- **Stream id, dropped.** One connection carries one message at a time, and responses come back in request order, as in HTTP/1.1 keep-alive. Position alone pairs each response with its request, so a stream id would cost 4 bytes per frame for information the receiver already has. The cost is head-of-line blocking: a large response delays every response behind it. We accept that for a file server.
- **No version field.** Version 1 needs none. A version 2 can add a HELLO frame type. A v1 server skips it and answers the request anyway, so the client learns the server is v1 by receiving a response with no HELLO before it.

The result is a 5-byte header. A typical GET is 53 bytes on the wire, against 85 for the same request in HTTP/1.1 text. Appendix: [HEXDUMP.md](HEXDUMP.md) annotates every byte of one complete exchange.
