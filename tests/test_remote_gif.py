"""`remote_gif`: the pasted GIF fetched from its public https address, SSRF-hardened. Fake DNS and a mock
transport: no network."""

import socket
import struct
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Callable
from typing import Any, NamedTuple

import httpx
import pytest

from miniteams import attachments as A

GIF = b"GIF89a" + struct.pack("<HH", 7, 5) + b"\x00" * 20
PUBLIC, OTHER = "93.184.216.34", "2606:4700::1111"
# The IDN names resolve: only the ASCII rule refuses them.
DNS = {
    "gifs.example": [PUBLIC],
    "v6.example": [OTHER],
    "cdn.example": ["8.8.8.8"],
    "ü.example": [PUBLIC],
    "bücher.example": [PUBLIC],
}


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Fake resolver over a copy of DNS; an unknown name fails like a real lookup."""
    table = {name: list(ips) for name, ips in DNS.items()}

    def fake(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        if host not in table:
            raise socket.gaierror("unknown")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in table[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


class Raw(httpx.AsyncByteStream):
    """A body as a server streams it: httpx neither reads it ahead nor decodes it unless asked."""

    def __init__(self, body: bytes | Callable[[], AsyncIterator[bytes]]) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if isinstance(self.body, bytes):
            yield self.body
        else:
            async for chunk in self.body():
                yield chunk


class R(NamedTuple):
    status: int
    body: bytes | Callable[[], AsyncIterator[bytes]] = b""
    headers: dict[str, str] = {}


class Remote:
    """Mock transport: `answers` by (pinned IP, path), every request recorded."""

    def __init__(self, answers: dict[tuple[str, str], R]) -> None:
        self.answers, self.asked = answers, []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        status, body, headers = self.answers.get((request.url.host, request.url.path), R(404))
        return httpx.Response(status, headers=headers, stream=Raw(body))


async def test_a_public_gif_is_fetched_from_the_vetted_ip_with_no_credentials(dns: Any) -> None:
    remote = Remote(
        {
            (PUBLIC, "/a.GIF"): R(200, body=GIF),
            (OTHER, "/b.gif"): R(200, body=GIF),
        }
    )
    assert await A.remote_gif("https://gifs.example/a.GIF?cid=1#x", remote.transport) == GIF
    assert await A.remote_gif("https://v6.example/b.gif", remote.transport) == GIF
    first, second = remote.asked
    assert str(first.url) == f"https://{PUBLIC}/a.GIF?cid=1"
    assert first.headers["host"] == "gifs.example" and first.extensions["sni_hostname"] == "gifs.example"
    assert str(second.url) == f"https://[{OTHER}]/b.gif" and second.extensions["sni_hostname"] == "v6.example"
    for request in remote.asked:
        assert not {"cookie", "authorization"} & set(request.headers)
        assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("ips", "dialled"), [([OTHER, PUBLIC], PUBLIC), ([OTHER, "8.8.8.8", PUBLIC], "8.8.8.8")]
)
async def test_dials_the_first_ipv4_of_a_dual_stack_host(dns: Any, ips: list[str], dialled: str) -> None:
    dns["gifs.example"] = ips
    remote = Remote({(ip, "/a.gif"): R(200, body=GIF) for ip in ips})
    assert await A.remote_gif("https://gifs.example/a.gif", remote.transport) == GIF
    assert [r.url.host for r in remote.asked] == [dialled]


@pytest.mark.parametrize(("literal", "host"), [(OTHER, f"[{OTHER}]"), ("8.8.4.4", "8.8.4.4")])
async def test_an_ip_literal_url_keeps_its_host_header_form(
    literal: str, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _numeric)
    remote = Remote({(literal, "/a.gif"): R(200, body=GIF)})
    assert await A.remote_gif(f"https://{host}/a.gif", remote.transport) == GIF
    [request] = remote.asked
    assert request.headers["host"] == host and request.extensions["sni_hostname"] == literal


@pytest.mark.parametrize(
    "url",
    [
        "http://gifs.example/a.gif",
        "ftp://gifs.example/a.gif",
        "https://user@gifs.example/a.gif",
        "https://user:pw@gifs.example/a.gif",  # pragma: allowlist secret
        "https://@gifs.example/a.gif",
        "https://gifs.example:8443/a.gif",
        "https://gifs.example:443/a.gif",
        "https://unknown.example/a.gif",
        "https:///a.gif",
        "not a url",
        "https://ü.example/a.gif",  # IDN: refused, never resolved nor sent as a raw Host
        "https://BÜCHER.example/a.gif",
        "https://gifs.example/a.png",
        "https://gifs.example/a.gif/x",
        "https://gifs.example/a?x=.gif",
        "https://gifs.example/a.gifv",
        "https://gifs.example/a\x01.gif",
        "https://gifs.example/a\x7f.gif",
    ],
)
async def test_refuses_other_schemes_userinfo_ports_paths_and_unresolved_hosts(dns: Any, url: str) -> None:
    remote = Remote({(PUBLIC, "/a.gif"): R(200, body=GIF)})
    assert await A.remote_gif(url, remote.transport) is None
    assert remote.asked == []


@pytest.mark.parametrize(
    "ips",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["192.168.1.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["::1"],
        ["fe80::1"],
        ["fc00::1"],
        ["::ffff:127.0.0.1"],
        ["::"],
        ["64:ff9b::7f00:1"],  # NAT64 of 127.0.0.1
        ["64:ff9b::a9fe:a9fe"],  # NAT64 of 169.254.169.254
        ["::7f00:1"],  # IPv4-compatible 127.0.0.1
        ["::a00:1"],  # IPv4-compatible 10.0.0.1
        ["64:ff9b:1::a00:1"],
        [PUBLIC, "10.0.0.1"],  # one private address among public ones is enough to refuse
        [],
    ],
)
async def test_refuses_a_host_resolving_to_any_non_public_address(dns: Any, ips: list[str]) -> None:
    dns["gifs.example"] = ips
    remote = Remote({(ip, "/a.gif"): R(200, body=GIF) for ip in ips})
    assert await A.remote_gif("https://gifs.example/a.gif", remote.transport) is None
    assert remote.asked == []


@pytest.mark.parametrize("ip", ["64:ff9b::808:808", "::808:808"])
async def test_an_ipv6_embedding_a_public_ipv4_passes(dns: Any, ip: str) -> None:
    dns["gifs.example"] = [ip]
    remote = Remote({(ip, "/a.gif"): R(200, body=GIF)})
    assert await A.remote_gif("https://gifs.example/a.gif", remote.transport) == GIF


@pytest.mark.parametrize(
    "literal", ["https://127.0.0.1/a.gif", "https://[::1]/a.gif", "https://169.254.169.254/a.gif"]
)
async def test_refuses_a_private_ip_literal(literal: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _numeric)
    remote = Remote({})
    assert await A.remote_gif(literal, remote.transport) is None
    assert remote.asked == []


def _numeric(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (host, port))]


async def test_redirects_are_vetted_hop_by_hop(dns: Any) -> None:
    dns["inside.example"] = ["10.0.0.7"]
    remote = Remote(
        {
            (PUBLIC, "/to-private.gif"): R(302, headers={"Location": "https://inside.example/a.gif"}),
            (PUBLIC, "/to-http.gif"): R(301, headers={"Location": "http://cdn.example/a.gif"}),
            (PUBLIC, "/to-cdn.gif"): R(302, headers={"Location": "https://cdn.example/x/a.gif"}),
            (PUBLIC, "/relative.gif"): R(307, headers={"Location": "/a.gif"}),
            (PUBLIC, "/a.gif"): R(200, body=GIF),
            ("8.8.8.8", "/x/a.gif"): R(200, body=GIF),
            (PUBLIC, "/loop.gif"): R(302, headers={"Location": "/loop.gif"}),
        }
    )
    assert await A.remote_gif("https://gifs.example/to-private.gif", remote.transport) is None
    assert await A.remote_gif("https://gifs.example/to-http.gif", remote.transport) is None
    assert [r.url.host for r in remote.asked] == [PUBLIC, PUBLIC]  # neither target was dialled
    assert await A.remote_gif("https://gifs.example/to-cdn.gif", remote.transport) == GIF
    assert remote.asked[-1].url.host == "8.8.8.8" and remote.asked[-1].headers["host"] == "cdn.example"
    assert await A.remote_gif("https://gifs.example/relative.gif", remote.transport) == GIF
    remote.asked.clear()
    assert await A.remote_gif("https://gifs.example/loop.gif", remote.transport) is None
    assert len(remote.asked) == A._REMOTE_HOPS + 1


@pytest.mark.parametrize("location", ["https://a]b/x.gif", "//[zz/a.gif", "https://a[b]/x.gif"])
async def test_an_unparsable_redirect_is_a_refusal(dns: Any, location: str) -> None:
    remote = Remote({(PUBLIC, "/a.gif"): R(302, headers={"Location": location})})
    assert await A.remote_gif("https://gifs.example/a.gif", remote.transport) is None
    assert len(remote.asked) == 1


async def test_refuses_oversize_non_gif_and_failed_answers(dns: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "MAX_IMAGE_BYTES", len(GIF))
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 3, 2) + b"\x00" * 8
    remote = Remote(
        {
            (PUBLIC, "/fit.gif"): R(200, body=GIF),
            (PUBLIC, "/big.gif"): R(200, body=GIF + b"\x00"),
            (PUBLIC, "/png.gif"): R(200, body=png),
            (PUBLIC, "/html.gif"): R(200, body=b"<script>x</script>", headers={"Content-Type": "image/gif"}),
            (PUBLIC, "/err.gif"): R(500, body=GIF),
        }
    )
    assert await A.remote_gif("https://gifs.example/fit.gif", remote.transport) == GIF
    for path in ("big", "png", "html", "err", "missing"):
        assert await A.remote_gif(f"https://gifs.example/{path}.gif", remote.transport) is None, path


async def test_a_transport_failure_is_a_refusal(dns: Any) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await A.remote_gif("https://gifs.example/a.gif", httpx.MockTransport(broken)) is None


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Every AsyncClient `remote_gif` builds."""
    made: list[httpx.AsyncClient] = []

    class Recorded(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            made.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", Recorded)
    return made


async def test_each_hop_runs_on_a_fresh_client_ignoring_the_environment(
    dns: Any, clients: list[httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No proxy from the environment, no cookie or connection carried from one hop to the next."""
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.1:3128")
    remote = Remote(
        {
            (PUBLIC, "/hop.gif"): R(
                302, headers={"Location": "https://cdn.example/a.gif", "Set-Cookie": "s=1; Domain=example"}
            ),
            ("8.8.8.8", "/a.gif"): R(200, body=GIF),
        }
    )
    assert await A.remote_gif("https://gifs.example/hop.gif", remote.transport) == GIF
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert all(client.trust_env is False for client in clients)
    assert "cookie" not in remote.asked[1].headers


@pytest.mark.parametrize("encoding", ["gzip", "GZIP", "deflate", "br", "gzip, identity"])
async def test_refuses_a_compressed_answer(dns: Any, encoding: str) -> None:
    remote = Remote({(PUBLIC, "/a.gif"): R(200, body=GIF, headers={"Content-Encoding": encoding})})
    assert await A.remote_gif("https://gifs.example/a.gif", remote.transport) is None


def _bomb(mib: int) -> bytes:
    """`mib` MiB of zeros after a GIF header, gzipped (~1 KB per MiB), built a MiB at a time."""
    packer = zlib.compressobj(9, zlib.DEFLATED, 31)
    parts = [packer.compress(GIF)] + [packer.compress(bytes(1 << 20)) for _ in range(mib)]
    return b"".join([*parts, packer.flush()])


@pytest.mark.parametrize("headers", [{"Content-Encoding": "gzip"}, {"Content-Encoding": "deflate"}, {}])
async def test_a_compression_bomb_is_never_inflated(dns: Any, headers: dict[str, str]) -> None:
    """The cap counts wire bytes: 200 MiB of zeros gzipped is refused without being inflated."""
    bomb = _bomb(200)
    remote = Remote({(PUBLIC, "/a.gif"): R(200, body=bomb, headers=headers)})
    tracemalloc.start()
    try:
        assert await A.remote_gif("https://gifs.example/a.gif", remote.transport) is None
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 << 20


async def test_the_cap_stops_the_stream_on_the_wire(dns: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "MAX_IMAGE_BYTES", 1 << 20)
    sent: list[int] = []

    async def endless() -> AsyncIterator[bytes]:
        yield GIF
        while True:
            sent.append(1)
            yield bytes(65536)

    remote = Remote({(PUBLIC, "/a.gif"): R(200, body=endless)})
    assert await A.remote_gif("https://gifs.example/a.gif", remote.transport) is None
    assert 16 <= len(sent) <= 18
