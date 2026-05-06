from hivemind.api.rooms import _apply_room_query_default
from hivemind.config import Settings
from hivemind.rooms import RoomCreateRequest


def test_omitted_room_query_pins_service_default():
    req = RoomCreateRequest(scope_agent_id="scope-a")

    _apply_room_query_default(
        req,
        Settings(default_query_agent="default-query-hermes"),
    )

    assert req.query_mode == "fixed"
    assert req.query_agent_id == "default-query-hermes"


def test_explicit_uploadable_room_query_bypasses_service_default():
    req = RoomCreateRequest(scope_agent_id="scope-a", query_mode="uploadable")

    _apply_room_query_default(
        req,
        Settings(default_query_agent="default-query-hermes"),
    )

    assert req.query_mode == "uploadable"
    assert req.query_agent_id is None


def test_omitted_room_query_stays_uploadable_without_service_default():
    req = RoomCreateRequest(scope_agent_id="scope-a")

    _apply_room_query_default(req, Settings(default_query_agent=""))

    assert req.query_mode == "uploadable"
    assert req.query_agent_id is None
