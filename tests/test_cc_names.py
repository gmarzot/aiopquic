"""Congestion-control names are checked against picoquic's registry, exact
match, wherever one enters: an unknown name would run with no congestion
control at all."""
import pytest

from aiopquic._binding._transport import TransportContext
from aiopquic.asyncio.webtransport import connect_webtransport
from aiopquic.quic.configuration import QuicConfiguration

KNOWN = ["newreno", "reno", "cubic", "dcubic", "fast", "bbr", "bbr1",
         "prague", "c4"]


@pytest.mark.parametrize("name", ["BBR1", "Reno", "bbr2", "", "bbr1 "])
def test_configuration_rejects_unknown(name):
    with pytest.raises(ValueError):
        QuicConfiguration(congestion_control_algorithm=name)


@pytest.mark.parametrize("name", KNOWN + [None])
def test_configuration_accepts_known(name):
    cfg = QuicConfiguration(congestion_control_algorithm=name)
    assert cfg.congestion_control_algorithm == name


def test_start_rejects_unknown_and_stays_unstarted():
    ctx = TransportContext()
    with pytest.raises(ValueError):
        ctx.start(alpn="h3", congestion_control_algorithm="BBR1")
    assert not ctx.started
    ctx.start(alpn="h3", congestion_control_algorithm="reno")
    try:
        assert ctx.started
    finally:
        ctx.stop()


async def test_connect_webtransport_rejects_unknown():
    cfg = QuicConfiguration()
    cfg.congestion_control_algorithm = "BBR1"
    with pytest.raises(ValueError):
        async with connect_webtransport("127.0.0.1", 9, "/wt", timeout=1.0,
                                        configuration=cfg):
            pass
