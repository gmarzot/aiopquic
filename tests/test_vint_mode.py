"""Buffer/StreamChain vint-mode dispatch (push_vint / pull_vint / vi64).

A Buffer or StreamChain carries a `vi64` flavor flag (default False =
RFC9000 2-bit-prefix varint; True = draft-18 vi64 leading-1-bits varint).
push_vint/pull_vint dispatch to the matching codec in C. This is a generic
"which varint flavor" selector over codecs the buffer already has — it
carries no protocol/MoQT knowledge.

These tests assert (a) dispatch picks the right codec, (b) the two flavors
genuinely DIFFER where the spec says they must (values >= 64) and AGREE
below, (c) corner cases: 1- and 9-byte extremes, RFC9000 overflow, non-
minimal accept, underflow, post-construction tagging, cross-chunk reads,
and that the default (untagged) buffer is byte-for-byte the RFC9000 path.
"""
import pytest

from aiopquic.buffer import Buffer, BufferReadError
from aiopquic._binding._streamchain import StreamChain


# --- helpers ---------------------------------------------------------

def _push(v, vi64):
    b = Buffer(capacity=16, vi64=vi64)
    b.push_vint(v)
    return bytes(b.data)


def _pull(raw, vi64):
    return Buffer(data=raw, vi64=vi64).pull_vint()


def _pull_chain(raw, vi64):
    sc = StreamChain(vi64=vi64)
    sc.extend(raw)
    return sc.pull_vint()


# Boundary + spread values that exercise every length class of both codecs.
ROUNDTRIP_VALUES = [
    0, 1, 37, 63,           # 1-byte in both
    64, 100, 127,           # 1-byte vi64, 2-byte RFC9000  (DIVERGE)
    128, 200, 16383,        # 2-byte both, different bytes  (DIVERGE)
    16384, 1 << 20,         # 3-byte vi64 vs 4-byte RFC9000
    (1 << 30) - 1,          # 4-byte RFC9000 max-ish
    1 << 32, (1 << 62) - 1,  # large; (1<<62)-1 is RFC9000's max value
]

# Values that fit RFC9000 (<= 2^62-1) — for the "both flavors round-trip"
# parametrization. vi64 goes higher (tested separately).
RFC9000_OK = ROUNDTRIP_VALUES


# --- dispatch correctness: push_vint/pull_vint == the chosen codec ---

@pytest.mark.parametrize("v", RFC9000_OK)
def test_push_vint_dispatches_to_correct_codec(v):
    # push_vint(vi64=True) must equal push_uint_vi64; (vi64=False) must
    # equal push_uint_var — exactly, byte for byte.
    bt = Buffer(capacity=16); bt.push_uint_vi64(v)
    bf = Buffer(capacity=16); bf.push_uint_var(v)
    assert _push(v, True) == bytes(bt.data)
    assert _push(v, False) == bytes(bf.data)


@pytest.mark.parametrize("v", RFC9000_OK)
def test_pull_vint_dispatches_to_correct_codec(v):
    assert _pull(_push(v, True), True) == v
    assert _pull(_push(v, False), False) == v


@pytest.mark.parametrize("v", RFC9000_OK)
def test_roundtrip_both_modes_buffer_and_chain(v):
    assert _pull(_push(v, True), True) == v
    assert _pull(_push(v, False), False) == v
    assert _pull_chain(_push(v, True), True) == v
    assert _pull_chain(_push(v, False), False) == v


# --- TRUE encoding divergence: the whole point of two flavors ---------

@pytest.mark.parametrize("v", [0, 1, 37, 63])
def test_flavors_agree_below_64(v):
    # 0..63 encode identically in both flavors (single byte) — this is why
    # small-value interop "worked" before the codec was correct.
    assert _push(v, True) == _push(v, False)


@pytest.mark.parametrize("v", [64, 100, 127, 128, 200, 16383, 16384, 1 << 20])
def test_flavors_diverge_at_or_above_64(v):
    # At/above 64 the two flavors MUST produce different bytes; a buffer
    # tagged with the wrong flavor would silently misencode.
    assert _push(v, True) != _push(v, False)


def test_explicit_divergence_vectors():
    # Hand-verified discriminators (vi64 hex, rfc9000 hex).
    cases = {
        64:  ("40",   "4040"),
        100: ("64",   "4064"),
        127: ("7f",   "407f"),
        128: ("8080", "4080"),
        200: ("80c8", "40c8"),
    }
    for v, (vi64_hex, rfc_hex) in cases.items():
        assert _push(v, True) == bytes.fromhex(vi64_hex), v
        assert _push(v, False) == bytes.fromhex(rfc_hex), v


# --- corner cases ----------------------------------------------------

def test_vi64_max_9_bytes():
    # vi64 reaches the full uint64 range (9 bytes); RFC9000 cannot.
    v = (1 << 64) - 1
    raw = _push(v, True)
    assert len(raw) == 9
    assert _pull(raw, True) == v
    assert _pull_chain(raw, True) == v


def test_value_above_rfc9000_max_needs_vi64():
    # 2^62 exceeds RFC9000's 2-bit-prefix range, so push_vint(vi64=False)
    # must reject it — while vi64 mode encodes it fine. Guards against a
    # silently-truncated large value on the wrong flavor.
    big = 1 << 62
    with pytest.raises(Exception):
        _push(big, False)
    raw = _push(big, True)
    assert _pull(raw, True) == big


def test_non_minimal_accept_on_pull_vint():
    # vi64 decode MUST accept non-minimal forms: 0x8025 == 0x25 == 37.
    assert _pull(bytes.fromhex("8025"), True) == 37
    assert _pull_chain(bytes.fromhex("8025"), True) == 37


def test_pull_vint_underflow_raises():
    with pytest.raises(BufferReadError):
        Buffer(data=b"", vi64=True).pull_vint()
    with pytest.raises(BufferReadError):
        Buffer(data=b"", vi64=False).pull_vint()
    # truncated multi-byte vi64 (declares 2 bytes, only 1 present)
    with pytest.raises(BufferReadError):
        Buffer(data=bytes.fromhex("80"), vi64=True).pull_vint()


# --- the vi64 flag itself --------------------------------------------

def test_default_buffer_is_rfc9000_inert():
    # No vi64 kwarg => RFC9000, byte-for-byte identical to push_uint_var.
    b = Buffer(capacity=16)
    assert b.vi64 is False
    b.push_vint(100)
    ref = Buffer(capacity=16); ref.push_uint_var(100)
    assert bytes(b.data) == bytes(ref.data) == bytes.fromhex("4064")


def test_vi64_property_get_set_changes_dispatch():
    # Settable after construction (the receive pattern: wrap bytes, then
    # tag with the session's flavor before reading).
    b = Buffer(capacity=16)
    b.vi64 = True
    b.push_vint(100)
    assert bytes(b.data) == bytes.fromhex("64")  # vi64, not 4064
    r = Buffer(data=bytes.fromhex("64"))
    r.vi64 = True
    assert r.pull_vint() == 100


def test_streamchain_default_and_property():
    sc = StreamChain()
    assert sc.vi64 is False
    sc.vi64 = True
    sc.extend(bytes.fromhex("64"))
    assert sc.pull_vint() == 100  # decoded as vi64 after tagging


def test_streamchain_pull_vint_across_chunk_boundary():
    # A 2-byte vi64 split across two extend() calls must reassemble.
    raw = _push(16383, True)  # 2-byte vi64 (bfff)
    assert len(raw) == 2
    sc = StreamChain(vi64=True)
    sc.extend(raw[:1])
    sc.extend(raw[1:])
    assert sc.pull_vint() == 16383
    # and the 9-byte max across many 1-byte chunks
    raw9 = _push((1 << 64) - 1, True)
    sc2 = StreamChain(vi64=True)
    for i in range(len(raw9)):
        sc2.extend(raw9[i:i + 1])
    assert sc2.pull_vint() == (1 << 64) - 1
