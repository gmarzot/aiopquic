"""vi64 variable-length integer codec (MoQT draft-18 §1.4.1).

vi64 uses the count of leading 1-bits in the first byte to indicate the
length (1-9 bytes); the bits after the first 0, plus subsequent bytes, are
the value (big-endian). Distinct from RFC 9000's 2-bit-prefix varint.
Non-minimal encodings MUST be accepted on decode; the encoder emits the
minimal length. Test vectors are draft-18 Table 2.
"""
import pytest

from aiopquic.buffer import Buffer, BufferReadError
from aiopquic._binding._streamchain import (
    StreamChain, encode_object_subgroup_vi64,
)


def _enc(v):
    b = Buffer(capacity=16)
    b.push_uint_vi64(v)
    return bytes(b.data)


def _dec(raw):
    return Buffer(data=raw).pull_uint_vi64()


def _dec_chain(raw):
    sc = StreamChain()
    sc.extend(raw)
    return sc.pull_uint_vi64()


# draft-18 Table 2 — all minimal except the 0x8025 non-minimal example.
TABLE2 = [
    ("25", 37),
    ("bbbd", 15293),
    ("ed7f3e7d", 226442877),
    ("faa1a0e403d8", 2893212287960),
    ("fc8998abc66bc0", 151288809941952),
    ("fefa318fa8e3ca11", 70423237261249041),
    ("ffffffffffffffffff", 18446744073709551615),
]


@pytest.mark.parametrize("hexstr,value", TABLE2)
def test_table2_decode(hexstr, value):
    raw = bytes.fromhex(hexstr)
    assert _dec(raw) == value
    assert _dec_chain(raw) == value


@pytest.mark.parametrize("hexstr,value", TABLE2)
def test_table2_encode_is_minimal(hexstr, value):
    # Every Table-2 vector except 0x8025 is the minimal encoding, so the
    # encoder must reproduce it exactly.
    assert _enc(value) == bytes.fromhex(hexstr)


def test_non_minimal_accept():
    # 0x8025 is a 2-byte non-minimal encoding of 37 (minimal is 0x25).
    assert _dec(bytes.fromhex("8025")) == 37
    assert _dec_chain(bytes.fromhex("8025")) == 37
    # 3-byte non-minimal 37 (0b110 prefix, len 3).
    assert _dec(bytes.fromhex("c00025")) == 37
    # 9-byte non-minimal 37.
    assert _dec(bytes.fromhex("ff0000000000000025")) == 37
    # The encoder still emits the minimal form.
    assert _enc(37) == bytes.fromhex("25")


# (length, max-value-at-that-length) — the encoder must pick this length
# for the boundary value and one less for value-1.
BOUNDARIES = [
    (1, (1 << 7) - 1),
    (2, (1 << 14) - 1),
    (3, (1 << 21) - 1),
    (4, (1 << 28) - 1),
    (5, (1 << 35) - 1),
    (6, (1 << 42) - 1),
    (7, (1 << 49) - 1),
    (8, (1 << 56) - 1),
    (9, (1 << 64) - 1),
]


@pytest.mark.parametrize("length,maxval", BOUNDARIES)
def test_encode_minimal_length(length, maxval):
    assert len(_enc(maxval)) == length
    # one past the previous range's max also lands here (lowest value
    # needing this many bytes), except length 1 which starts at 0.
    if length > 1:
        first_at_len = (1 << (7 * (length - 1)))
        assert len(_enc(first_at_len)) == length


@pytest.mark.parametrize("v", [
    0, 1, 2, 127, 128, 16383, 16384, 2097151, 2097152,
    (1 << 28) - 1, 1 << 28, (1 << 35) - 1, 1 << 35,
    (1 << 42) - 1, 1 << 42, (1 << 49) - 1, 1 << 49,
    (1 << 56) - 1, 1 << 56, (1 << 64) - 1,
])
def test_round_trip(v):
    raw = _enc(v)
    assert _dec(raw) == v
    assert _dec_chain(raw) == v


def test_round_trip_multi_in_one_buffer():
    b = Buffer(capacity=64)
    vals = [0, 37, 15293, 1 << 40, (1 << 64) - 1, 5]
    for v in vals:
        b.push_uint_vi64(v)
    inp = Buffer(data=b.data)
    assert [inp.pull_uint_vi64() for _ in vals] == vals


def test_truncated_decode_raises():
    # First byte 0x80 claims 2 bytes but only 1 is present.
    with pytest.raises(BufferReadError):
        Buffer(data=bytes.fromhex("80")).pull_uint_vi64()
    # 0xFF claims 9 bytes; supply 4.
    with pytest.raises(BufferReadError):
        Buffer(data=bytes.fromhex("ff00112233")).pull_uint_vi64()
    # Empty buffer.
    with pytest.raises(BufferReadError):
        Buffer(data=b"").pull_uint_vi64()


def test_streamchain_cross_chunk_boundary():
    # Split a 9-byte vi64 across two chunks to exercise the cross-chunk
    # assembly path.
    raw = bytes.fromhex("fefa318fa8e3ca11")  # 8-byte value
    sc = StreamChain()
    sc.extend(raw[:3])
    sc.extend(raw[3:])
    assert sc.pull_uint_vi64() == 70423237261249041


# --- object-subgroup body twins (parse/encode_object_subgroup_vi64) ---

def _roundtrip_obj(delta, exts, status, payload, ext_present):
    raw = encode_object_subgroup_vi64(delta, exts, status, payload, ext_present)
    sc = StreamChain()
    sc.extend(raw)
    return sc.parse_object_subgroup_vi64(ext_present, 1 << 20)


def test_obj_vi64_normal_no_ext():
    assert _roundtrip_obj(5, None, 0, b"hello", False) == (5, None, 0, b"hello")


def test_obj_vi64_with_extensions():
    # ext_id even -> varint value; odd -> length-prefixed bytes.
    d, e, s, p = _roundtrip_obj(7, {2: 42, 3: b"abc"}, 0, b"data", True)
    assert (d, s, p) == (7, 0, b"data")
    assert e == {2: 42, 3: b"abc"}


def test_obj_vi64_non_normal_status_empty_payload():
    # status != 0 -> empty ext block + payload_len 0 + status.
    assert _roundtrip_obj(1, None, 3, b"", True) == (1, None, 3, b"")


def test_obj_vi64_large_delta_multibyte():
    d, e, s, p = _roundtrip_obj(1 << 40, None, 0, b"x", False)
    assert (d, e, s, p) == (1 << 40, None, 0, b"x")


def test_obj_vi64_large_extension_value():
    # vi64-encoded even-ext value spanning multiple bytes.
    d, e, s, p = _roundtrip_obj(0, {4: 2 ** 50}, 0, b"p", True)
    assert e == {4: 2 ** 50}
    assert (d, s, p) == (0, 0, b"p")
