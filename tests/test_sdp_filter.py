"""Tests for the ICE-candidate SDP filter.

The filter strips a=candidate: lines whose IP sits inside any of the
configured drop networks. Default usage drops Docker bridge gateways
(172.16.0.0/12) so the offer SDP doesn't bloat past what the TS6
server can reliably forward.
"""

from __future__ import annotations

from ts6_stream_bot.pipeline.stream_publisher import _filter_sdp_candidates

_SDP = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 0.0.0.0\r\n"
    "s=-\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 97\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=candidate:1 1 udp 2122260223 193.34.69.21 47453 typ host\r\n"
    "a=candidate:2 1 udp 2122260223 172.18.0.1 51882 typ host\r\n"
    "a=candidate:3 1 udp 2122260223 172.17.0.1 35938 typ host\r\n"
    "a=candidate:4 1 udp 1686052607 193.34.69.21 35938 typ srflx raddr 172.17.0.1 rport 35938\r\n"
    "a=ice-ufrag:abcd\r\n"
    "a=fingerprint:sha-256 AA:BB\r\n"
)


def test_drops_candidates_inside_network() -> None:
    """All host candidates in 172.16/12 should be removed."""
    rewritten, dropped = _filter_sdp_candidates(_SDP, ["172.16.0.0/12"])

    # The dropped candidate lines themselves are gone.
    assert "172.18.0.1 51882" not in rewritten
    assert "172.17.0.1 35938 typ host" not in rewritten

    # Public host + srflx (which advertises the public IP as its
    # connect address; the raddr=172.17.0.1 metadata is preserved
    # because the filter keys on the candidate IP, not the raddr).
    assert rewritten.count("a=candidate:") == 2
    assert "193.34.69.21 47453" in rewritten
    assert "193.34.69.21 35938 typ srflx" in rewritten

    dropped_ips = sorted(d["ip"] for d in dropped)
    assert dropped_ips == ["172.17.0.1", "172.18.0.1"]


def test_preserves_non_candidate_lines() -> None:
    """Filtering candidate lines must not touch the rest of the SDP."""
    rewritten, _ = _filter_sdp_candidates(_SDP, ["172.16.0.0/12"])

    for line in ("v=0", "o=- 1 1", "m=video", "a=ice-ufrag:abcd", "a=fingerprint:sha-256"):
        assert line in rewritten


def test_keeps_crlf_line_endings() -> None:
    """WebRTC SDPs are CRLF-terminated; aiortc / browsers parse strictly."""
    rewritten, _ = _filter_sdp_candidates(_SDP, ["172.16.0.0/12"])
    # Every retained line ends with CRLF.
    assert rewritten.endswith("\r\n")
    assert "\n\n" not in rewritten  # no orphaned LF-only line endings


def test_empty_network_list_passes_sdp_through() -> None:
    rewritten, dropped = _filter_sdp_candidates(_SDP, [])
    assert rewritten == _SDP
    assert dropped == []


def test_invalid_cidr_is_logged_and_skipped() -> None:
    """A typo in .env shouldn't crash the join flow."""
    rewritten, dropped = _filter_sdp_candidates(_SDP, ["not-a-cidr", "172.16.0.0/12"])
    # Valid network still applied.
    assert "172.18.0.1" not in rewritten
    assert {d["ip"] for d in dropped} == {"172.17.0.1", "172.18.0.1"}


def test_no_match_returns_full_sdp() -> None:
    rewritten, dropped = _filter_sdp_candidates(_SDP, ["10.0.0.0/8"])
    assert rewritten == _SDP
    assert dropped == []


def test_ipv6_drop_network() -> None:
    """The filter should also handle IPv6 networks correctly."""
    sdp = (
        "v=0\r\n"
        "a=candidate:1 1 udp 2122131711 fd75:cbb5:9606::1 50226 typ host\r\n"
        "a=candidate:2 1 udp 2121998079 192.168.178.29 50223 typ host\r\n"
    )
    rewritten, dropped = _filter_sdp_candidates(sdp, ["fd00::/8"])
    assert "fd75:cbb5:9606::1" not in rewritten
    assert "192.168.178.29" in rewritten
    assert [d["ip"] for d in dropped] == ["fd75:cbb5:9606::1"]
