# One complete exchange, byte by byte

Captured with:

```
./bserve ./www 9000
./bcurl -v localhost:9000/hello.txt
```

There are three frames on one connection: the client sends a REQUEST, and the server sends a RESPONSE and one DATA frame. Offsets are from the first byte of each frame, in hex. Field names refer to [SPEC.md](SPEC.md).

## Frame 1: REQUEST, client → server (53 bytes)

```
00000000  00 00 30 01 01 01 00 0a  2f 68 65 6c 6c 6f 2e 74  |..0...../hello.t|
00000010  78 74 01 00 0e 6c 6f 63  61 6c 68 6f 73 74 3a 39  |xt...localhost:9|
00000020  30 30 30 02 00 09 62 63  75 72 6c 2f 31 2e 30 03  |000...bcurl/1.0.|
00000030  00 03 2a 2f 2a                                    |..*/*|
```

| Offset | Bytes | Field | Meaning |
|--------|-------|-------|---------|
| 00–02 | `00 00 30` | Length | payload is 0x30 = **48** bytes |
| 03 | `01` | Type | REQUEST |
| 04 | `01` | Flags | END_MESSAGE: no body follows |
| 05 | `01` | method | GET |
| 06–07 | `00 0a` | path_len | 10 |
| 08–11 | `2f 68 … 74` | path | `/hello.txt` |
| 12 | `01` | header index | 1 = `host` |
| 13–14 | `00 0e` | value_len | 14 |
| 15–22 | `6c 6f … 30` | value | `localhost:9000` |
| 23 | `02` | header index | 2 = `user-agent` |
| 24–25 | `00 09` | value_len | 9 |
| 26–2e | `62 63 … 30` | value | `bcurl/1.0` |
| 2f | `03` | header index | 3 = `accept` |
| 30–31 | `00 03` | value_len | 3 |
| 32–34 | `2a 2f 2a` | value | `*/*` |

The payload ends exactly at 5 + 48 = 0x35 bytes, so the header block ends there too; there is no header count. The same request in HTTP/1.1 text is 85 bytes. The 32 bytes saved come from three one-byte header indexes (instead of `Host: `, `User-Agent: `, `Accept: `), the method byte, and dropping ` HTTP/1.1` and every `\r\n`.

## Frame 2: RESPONSE, server → client (141 bytes)

```
00000000  00 00 88 02 00 00 c8 04  00 0a 62 73 65 72 76 65  |..........bserve|
00000010  2f 31 2e 30 05 00 1d 54  68 75 2c 20 30 38 20 4f  |/1.0...Thu, 08 O|
00000020  63 74 20 32 30 32 36 20  31 33 3a 30 38 3a 30 37  |ct 2026 13:08:07|
00000030  20 47 4d 54 06 00 19 74  65 78 74 2f 70 6c 61 69  | GMT...text/plai|
00000040  6e 3b 20 63 68 61 72 73  65 74 3d 75 74 66 2d 38  |n; charset=utf-8|
00000050  07 00 02 32 30 08 00 1d  54 68 75 2c 20 30 38 20  |...20...Thu, 08 |
00000060  4f 63 74 20 32 30 32 36  20 31 33 3a 30 36 3a 35  |Oct 2026 13:06:5|
00000070  35 20 47 4d 54 09 00 15  22 31 34 2d 31 38 64 63  |5 GMT..."14-18dc|
00000080  38 66 63 32 31 31 39 30  61 34 61 33 22           |8fc21190a4a3"|
```

| Offset | Bytes | Field | Meaning |
|--------|-------|-------|---------|
| 00–02 | `00 00 88` | Length | payload is 0x88 = **136** bytes |
| 03 | `02` | Type | RESPONSE |
| 04 | `00` | Flags | END_MESSAGE **not** set: DATA frames follow |
| 05–06 | `00 c8` | status | 0xc8 = **200** |
| 07 | `04` | header index | 4 = `server` |
| 08–09 | `00 0a` | value_len | 10 |
| 0a–13 | `62 73 … 30` | value | `bserve/1.0` |
| 14 | `05` | header index | 5 = `date` |
| 15–16 | `00 1d` | value_len | 29 |
| 17–33 | `54 68 … 54` | value | `Thu, 08 Oct 2026 13:08:07 GMT` |
| 34 | `06` | header index | 6 = `content-type` |
| 35–36 | `00 19` | value_len | 25 |
| 37–4f | `74 65 … 38` | value | `text/plain; charset=utf-8` |
| 50 | `07` | header index | 7 = `content-length` |
| 51–52 | `00 02` | value_len | 2 |
| 53–54 | `32 30` | value | `20`, the body size in bytes |
| 55 | `08` | header index | 8 = `last-modified` |
| 56–57 | `00 1d` | value_len | 29 |
| 58–74 | `54 68 … 54` | value | `Thu, 08 Oct 2026 13:06:55 GMT` |
| 75 | `09` | header index | 9 = `etag` |
| 76–77 | `00 15` | value_len | 21 |
| 78–8c | `22 31 … 22` | value | `"14-18dc8fc21190a4a3"`, i.e. size (0x14) and mtime in ns |

Check: 2 (status) + 13 + 32 + 28 + 5 + 32 + 24 (headers) = 136 = Length.

## Frame 3: DATA, server → client (25 bytes)

```
00000000  00 00 14 00 01 68 65 6c  6c 6f 2c 20 62 69 6e 61  |.....hello, bina|
00000010  72 79 20 77 6f 72 6c 64  0a                       |ry world.|
```

| Offset | Bytes | Field | Meaning |
|--------|-------|-------|---------|
| 00–02 | `00 00 14` | Length | 0x14 = **20** bytes |
| 03 | `00` | Type | DATA |
| 04 | `01` | Flags | END_MESSAGE: the response is complete |
| 05–18 | `68 65 … 0a` | payload | `hello, binary world\n` |

The client prints the 20 payload bytes to stdout and exits 0. The connection is still open at this point, so another request could follow on it. bcurl closes it only because it has no more targets.

## A frame the receiver does not know

`bcurl --grease` sends this before each request:

```
00000000  00 00 07 fa 00 73 6b 69  70 20 6d 65              |.....skip me|
```

The fields are Length 7, Type `0xfa` (reserved, never assigned) and Flags 0. bserve reads the 5-byte header, discards the next 7 bytes, and reads the following REQUEST as usual.
