from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from hivemind.api.room_helpers import room_link, share_room_link


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("internal", 8100),
            "path": "/v1/rooms",
            "headers": raw_headers,
        }
    )


def _service(link: str) -> str:
    parsed = urlparse(link)
    return parse_qs(parsed.query)["service"][0]


def test_room_link_uses_forwarded_https_origin():
    link = room_link(
        _request(
            {
                "host": "hivemind.teleport.computer",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "hivemind.teleport.computer",
            }
        ),
        "room_abc",
        "hmq_token",
        "pubkey",
    )

    parsed = urlparse(link)
    assert parsed.netloc == "hivemind.teleport.computer"
    assert _service(link) == "https://hivemind.teleport.computer"


def test_share_room_link_uses_forwarded_header_origin():
    link = share_room_link(
        _request(
            {
                "host": "internal:8100",
                "forwarded": 'for=10.0.0.1;proto=https;host="rooms.example"',
            }
        ),
        "room_abc",
        "hms_token",
        "pubkey",
    )

    parsed = urlparse(link)
    assert parsed.netloc == "rooms.example"
    assert _service(link) == "https://rooms.example"


def test_room_link_keeps_local_http_origin_without_forwarded_headers():
    link = room_link(
        _request({"host": "rooms"}),
        "room_abc",
        "hmq_token",
        "pubkey",
    )

    parsed = urlparse(link)
    assert parsed.netloc == "rooms"
    assert _service(link) == "http://rooms"
