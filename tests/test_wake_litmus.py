"""Lost-wake litmus: each wake protocol's two sides raced on two threads.

A lost wake leaves a writer parked or ring entries unseen with nobody to
wake for them; under load it shows up as a stall that only times out.
AIOPQUIC_LITMUS_N raises the round count for a longer soak.
"""
import os

import pytest

from aiopquic._binding._transport import _litmus

ROUNDS = int(os.environ.get("AIOPQUIC_LITMUS_N", "200000"))


@pytest.mark.parametrize("kind", [
    "rx_notify", "tx_wake", "rx_event", "tx_drain", "tx_ring", "dgram",
    "rx_drained",
])
def test_no_lost_wake(kind):
    assert _litmus(kind, ROUNDS) == 0
