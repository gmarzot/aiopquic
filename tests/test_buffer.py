"""Unit tests for aiopquic.buffer.Buffer (Cython).

Round-trip + boundary-value coverage of the QUIC varint codec and
push/pull primitives. Mirrors what aiomoqt's serializer/parser
actually exercises.
"""
import pytest

from aiopquic.buffer import Buffer, BufferReadError


# ---- varint round-trip ---------------------------------------------

VARINT_BOUNDARIES = [
    0,
    1,
    62,
    63,                  # max 1-byte
    64,                  # min 2-byte
    255,
    256,
    16383,               # max 2-byte
    16384,               # min 4-byte
    65535,
    65536,
    1073741822,
    1073741823,          # max 4-byte
    1073741824,          # min 8-byte
    (1 << 48) - 1,
    (1 << 62) - 2,
    (1 << 62) - 1,       # max 8-byte
]


@pytest.mark.parametrize("v", VARINT_BOUNDARIES)
def test_uint_var_roundtrip(v):
    b = Buffer(capacity=16)
    b.push_uint_var(v)
    n = b.tell()
    expected_n = (1 if v < (1 << 6) else 2 if v < (1 << 14)
                  else 4 if v < (1 << 30) else 8)
    assert n == expected_n, f"v={v} took {n} bytes, expected {expected_n}"

    b2 = Buffer(data=b.data)
    assert b2.pull_uint_var() == v
    assert b2.tell() == n


def test_uint_var_too_large():
    b = Buffer(capacity=16)
    with pytest.raises(ValueError):
        b.push_uint_var(1 << 62)


# ---- fixed-width uints ---------------------------------------------

def test_uint8_roundtrip():
    b = Buffer(capacity=16)
    for v in [0, 1, 127, 128, 254, 255]:
        b.push_uint8(v)
    b2 = Buffer(data=b.data)
    for v in [0, 1, 127, 128, 254, 255]:
        assert b2.pull_uint8() == v


def test_uint16_roundtrip():
    b = Buffer(capacity=16)
    for v in [0, 1, 0x1234, 0xFFFE, 0xFFFF]:
        b.push_uint16(v)
    b2 = Buffer(data=b.data)
    for v in [0, 1, 0x1234, 0xFFFE, 0xFFFF]:
        assert b2.pull_uint16() == v


def test_uint32_roundtrip():
    b = Buffer(capacity=16)
    for v in [0, 1, 0x12345678, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFE,
              0xFFFFFFFF]:
        b.push_uint32(v)
    b2 = Buffer(data=b.data)
    for v in [0, 1, 0x12345678, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFE,
              0xFFFFFFFF]:
        assert b2.pull_uint32() == v


def test_uint64_roundtrip():
    b = Buffer(capacity=64)
    for v in [0, 1, 0x123456789ABCDEF0, (1 << 63) - 1, (1 << 64) - 1]:
        b.push_uint64(v)
    b2 = Buffer(data=b.data)
    for v in [0, 1, 0x123456789ABCDEF0, (1 << 63) - 1, (1 << 64) - 1]:
        assert b2.pull_uint64() == v


# ---- bytes ---------------------------------------------------------

def test_push_bytes_roundtrip():
    payload = b"hello, MoQT"
    b = Buffer(capacity=64)
    b.push_uint_var(len(payload))
    b.push_bytes(payload)
    b2 = Buffer(data=b.data)
    n = b2.pull_uint_var()
    assert n == len(payload)
    assert b2.pull_bytes(n) == payload


def test_push_bytearray_and_memoryview():
    b = Buffer(capacity=64)
    b.push_bytes(bytearray(b"abc"))
    b.push_bytes(memoryview(b"def"))
    assert b.data == b"abcdef"


def test_push_empty_bytes():
    b = Buffer(capacity=64)
    b.push_bytes(b"")
    assert b.tell() == 0


# ---- growth --------------------------------------------------------

def test_growable_default():
    b = Buffer()
    big = b"x" * 4096
    b.push_bytes(big)
    assert b.data == big
    assert b.capacity >= 4096


def test_explicit_growth_past_initial_capacity():
    b = Buffer(capacity=4)
    b.push_uint_var(1)
    b.push_bytes(b"y" * 1000)
    assert b.tell() == 1 + 1000
    assert b.data[0] == 1


def test_fixed_capacity_read_mode_no_growth():
    b = Buffer(data=b"abc")
    with pytest.raises(BufferReadError):
        b.push_uint8(1)


# ---- bounds + cursor ----------------------------------------------

def test_pull_past_end():
    b = Buffer(data=b"\x00")
    b.pull_uint8()
    with pytest.raises(BufferReadError):
        b.pull_uint8()


def test_pull_uint16_short_buffer():
    b = Buffer(data=b"\x00")
    with pytest.raises(BufferReadError):
        b.pull_uint16()


def test_pull_uint_var_short_buffer():
    b = Buffer(data=b"\x80")  # signals 4-byte varint, only 1 byte present
    with pytest.raises(BufferReadError):
        b.pull_uint_var()


def test_seek_and_data_slice():
    b = Buffer(capacity=16)
    b.push_uint_var(0xABCD)
    b.push_bytes(b"data")
    end = b.tell()
    assert b.data_slice(0, end) == b.data
    b.seek(2)  # cursor in middle
    assert b.tell() == 2


def test_seek_out_of_bounds():
    b = Buffer(capacity=8)
    with pytest.raises(BufferReadError):
        b.seek(-1)
    with pytest.raises(BufferReadError):
        b.seek(b.capacity + 1)


def test_eof():
    b = Buffer(data=b"x")
    assert not b.eof()
    b.pull_uint8()
    assert b.eof()


# ---- realistic MoQT-like serialization round-trip -----------------

def test_moqt_like_message():
    # type=0x20, payload_len, name="bench/track", priority=128
    out = Buffer(capacity=128)
    out.push_uint_var(0x20)
    out.push_uint_var(11)         # name length
    out.push_bytes(b"bench/track")
    out.push_uint8(128)
    out.push_uint_var(0)          # extensions empty

    inp = Buffer(data=out.data)
    assert inp.pull_uint_var() == 0x20
    n = inp.pull_uint_var()
    assert n == 11
    assert inp.pull_bytes(n) == b"bench/track"
    assert inp.pull_uint8() == 128
    assert inp.pull_uint_var() == 0
    assert inp.eof()
