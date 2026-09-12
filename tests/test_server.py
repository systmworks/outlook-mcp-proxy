import time

import httpx
import pytest
import respx

import server


@pytest.fixture(autouse=True)
async def http_client():
    server._http_client = httpx.AsyncClient(timeout=5)
    server._session_jti.set("fake-jti")
    server._token_store["fake-jti"] = {
        "access_token": "fake-token",
        "refresh_token": "fake-refresh",
        "expiry": time.time() + 3600,
        "email": "test@example.com",
        "read_only": False,
        "jwt_exp": time.time() + 86400,
    }
    server._read_only.set(False)
    yield
    await server._http_client.aclose()
    server._http_client = None
    server._token_store.pop("fake-jti", None)


# ── read tools ─────────────────────────────────────────────────────────────

@respx.mock
async def test_get_profile_returns_graph_response():
    respx.get(server.ME).mock(return_value=httpx.Response(200, json={
        "id": "u1", "displayName": "Test User", "mail": "a@example.com",
    }))
    result = await server.get_profile()
    assert result["mail"] == "a@example.com"


@respx.mock
async def test_search_emails_wraps_query_in_quotes_and_returns_hits():
    route = respx.get(f"{server.ME}/messages").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "m1", "subject": "hi"}],
    }))
    results = await server.search_emails("from:a@example.com")
    assert results == [{"id": "m1", "subject": "hi"}]
    sent = route.calls.last.request.url.params["$search"]
    assert sent == '"from:a@example.com"'


@respx.mock
async def test_read_message_decodes_body_and_recipients_without_mime_walking():
    respx.get(f"{server.ME}/messages/m1").mock(return_value=httpx.Response(200, json={
        "id": "m1", "conversationId": "c1",
        "from": {"emailAddress": {"address": "a@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "b@example.com"}}],
        "ccRecipients": [],
        "subject": "Hi", "receivedDateTime": "2026-01-01T00:00:00Z",
        "bodyPreview": "hi there",
        "body": {"contentType": "text", "content": "hi there, full body"},
        "categories": [], "parentFolderId": "f1", "hasAttachments": False,
    }))
    msg = await server.read_message("m1")
    assert msg["from"] == "a@example.com"
    assert msg["to"] == ["b@example.com"]
    assert msg["body"] == "hi there, full body"
    assert msg["attachments"] == []


@respx.mock
async def test_read_message_fetches_attachments_when_present_and_excludes_inline():
    respx.get(f"{server.ME}/messages/m1").mock(return_value=httpx.Response(200, json={
        "id": "m1", "conversationId": "c1", "from": None, "toRecipients": [],
        "ccRecipients": [], "subject": "Hi", "receivedDateTime": "2026-01-01T00:00:00Z",
        "bodyPreview": "", "body": {"contentType": "text", "content": ""},
        "categories": [], "parentFolderId": "f1", "hasAttachments": True,
    }))
    respx.get(f"{server.ME}/messages/m1/attachments").mock(return_value=httpx.Response(200, json={
        "value": [
            {"id": "a1", "name": "file.pdf", "contentType": "application/pdf",
             "size": 100, "isInline": False},
            {"id": "a2", "name": "logo.png", "contentType": "image/png",
             "size": 50, "isInline": True},
        ],
    }))
    msg = await server.read_message("m1")
    assert len(msg["attachments"]) == 1
    assert msg["attachments"][0]["attachmentId"] == "a1"


@respx.mock
async def test_read_conversation_orders_by_received_date_time():
    route = respx.get(f"{server.ME}/messages").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "m1"}, {"id": "m2"}],
    }))
    results = await server.read_conversation("c1")
    assert [m["id"] for m in results] == ["m1", "m2"]
    params = route.calls.last.request.url.params
    assert params["$filter"] == "conversationId eq 'c1'"
    assert params["$orderby"] == "receivedDateTime asc"


@respx.mock
async def test_get_attachment_checks_declared_size_before_downloading():
    server.ATTACHMENT_MAX_MB, orig = 1, server.ATTACHMENT_MAX_MB
    server.ATTACHMENT_MAX_BYTES, orig_bytes = 1 * 1024 * 1024, server.ATTACHMENT_MAX_BYTES
    try:
        respx.get(f"{server.ME}/messages/m1/attachments/a1").mock(return_value=httpx.Response(200, json={
            "name": "big.zip", "contentType": "application/zip", "size": 5 * 1024 * 1024,
        }))
        with pytest.raises(ValueError, match="exceeds"):
            await server.get_attachment("m1", "a1")
    finally:
        server.ATTACHMENT_MAX_MB = orig
        server.ATTACHMENT_MAX_BYTES = orig_bytes


@respx.mock
async def test_get_attachment_reuses_id_across_calls_no_gmail_style_mismatch():
    # Unlike Gmail's attachmentId (which can differ across separate messages.get
    # calls for the same attachment), Graph attachment ids are stable — the same
    # id returned by an earlier read_message/list call can always be reused here.
    import base64
    content = base64.b64encode(b"hello world").decode()
    route = respx.get(f"{server.ME}/messages/m1/attachments/a1")
    route.mock(side_effect=[
        httpx.Response(200, json={"name": "f.txt", "contentType": "text/plain", "size": 11}),
        httpx.Response(200, json={"name": "f.txt", "contentType": "text/plain", "size": 11,
                                  "contentBytes": content}),
    ])
    result = await server.get_attachment("m1", "a1")
    assert result["attachmentId"] == "a1"
    assert result["filename"] == "f.txt"
    assert base64.b64decode(result["data"]) == b"hello world"


# ── write tools: read-only enforcement ───────────────────────────────────────

_WRITE_TOOL_CALLS = [
    ("send_email", ("a@example.com", "Subj", "Body")),
    ("create_draft", ("a@example.com", "Subj", "Body")),
    ("send_draft", ("d1",)),
    ("update_draft", ("d1", "a@example.com", "Subj", "Body")),
    ("delete_draft", ("d1",)),
    ("create_folder", ("New Folder",)),
    ("update_folder", ("f1", "Renamed")),
    ("delete_folder", ("f1",)),
    ("update_categories", ("m1",)),
    ("move_message", ("m1", "archive")),
    ("mark_as_junk", ("m1",)),
    ("trash_message", ("m1",)),
]


@pytest.mark.parametrize("tool_name,args", _WRITE_TOOL_CALLS)
async def test_write_tools_reject_read_only_sessions(tool_name, args):
    server._read_only.set(True)
    tool = getattr(server, tool_name)
    with pytest.raises(PermissionError):
        await tool(*args)


# ── folders / categories / move ──────────────────────────────────────────────

@respx.mock
async def test_list_folders_defaults_to_top_level_only():
    respx.get(f"{server.ME}/mailFolders").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "top", "displayName": "Top", "childFolderCount": 1}],
    }))
    # No mock registered for .../top/childFolders — if list_folders() walked
    # into it by default, respx would raise for the unmocked request.
    folders = await server.list_folders()
    assert [f["id"] for f in folders] == ["top"]


@respx.mock
async def test_list_folders_recursive_walks_nested_children():
    respx.get(f"{server.ME}/mailFolders").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "top", "displayName": "Top", "childFolderCount": 1}],
    }))
    respx.get(f"{server.ME}/mailFolders/top/childFolders").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "child", "displayName": "Child", "childFolderCount": 0}],
    }))
    folders = await server.list_folders(recursive=True)
    ids = {f["id"] for f in folders}
    assert ids == {"top", "child"}


@respx.mock
async def test_list_folders_parent_folder_id_lists_one_level_of_children():
    route = respx.get(f"{server.ME}/mailFolders/parent1/childFolders").mock(
        return_value=httpx.Response(200, json={
            "value": [{"id": "child1", "displayName": "Child 1", "childFolderCount": 0}],
        })
    )
    folders = await server.list_folders(parent_folder_id="parent1")
    assert [f["id"] for f in folders] == ["child1"]
    assert route.called


@respx.mock
async def test_list_folders_name_contains_filters_case_insensitively():
    respx.get(f"{server.ME}/mailFolders").mock(return_value=httpx.Response(200, json={
        "value": [
            {"id": "a", "displayName": "Lot 033", "childFolderCount": 0},
            {"id": "b", "displayName": "Correspondence", "childFolderCount": 0},
        ],
    }))
    folders = await server.list_folders(name_contains="lot 033")
    assert [f["id"] for f in folders] == ["a"]


@respx.mock
async def test_list_folders_caps_result_at_safety_limit():
    many = [{"id": str(i), "displayName": f"Folder {i}", "childFolderCount": 0} for i in range(500)]
    respx.get(f"{server.ME}/mailFolders").mock(return_value=httpx.Response(200, json={"value": many}))
    folders = await server.list_folders()
    assert len(folders) == server._LIST_FOLDERS_MAX


@respx.mock
async def test_update_categories_merges_add_and_remove():
    respx.get(f"{server.ME}/messages/m1").mock(return_value=httpx.Response(200, json={
        "categories": ["Red", "Blue"],
    }))
    route = respx.patch(f"{server.ME}/messages/m1").mock(
        return_value=httpx.Response(200, json={"categories": ["Blue", "Green"]})
    )
    result = await server.update_categories("m1", add=["Green"], remove=["Red"])
    sent = route.calls.last.request.content
    import json
    body = json.loads(sent)
    assert sorted(body["categories"]) == ["Blue", "Green"]
    assert result["categories"] == ["Blue", "Green"]


@respx.mock
async def test_move_message_returns_new_message_id():
    # Graph assigns a NEW id on move — unlike Gmail, where message ids never
    # change when labels/location change.
    respx.post(f"{server.ME}/messages/m1/move").mock(return_value=httpx.Response(200, json={
        "id": "m1-new-id-after-move",
    }))
    result = await server.move_message("m1", "archive")
    assert result["id"] == "m1-new-id-after-move"
    assert result["id"] != "m1"


@respx.mock
async def test_mark_as_junk_moves_to_well_known_junk_folder():
    route = respx.post(f"{server.ME}/messages/m1/move").mock(
        return_value=httpx.Response(200, json={"id": "m2"})
    )
    await server.mark_as_junk("m1")
    import json
    body = json.loads(route.calls.last.request.content)
    assert body["destinationId"] == "junkemail"


@respx.mock
async def test_trash_message_moves_to_deleted_items():
    route = respx.post(f"{server.ME}/messages/m1/move").mock(
        return_value=httpx.Response(200, json={"id": "m2"})
    )
    await server.trash_message("m1")
    import json
    body = json.loads(route.calls.last.request.content)
    assert body["destinationId"] == "deleteditems"


@respx.mock
async def test_delete_draft_handles_204_no_content():
    respx.delete(f"{server.ME}/messages/d1").mock(return_value=httpx.Response(204))
    result = await server.delete_draft("d1")
    assert result == {"deleted": "d1"}


# ── calendar ──────────────────────────────────────────────────────────────

@respx.mock
async def test_list_events_uses_calendar_view_not_plain_events():
    # calendarView correctly expands recurring events into instances within the
    # window; a plain /events listing would not.
    route = respx.get(f"{server.ME}/calendarView").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "e1"}],
    }))
    results = await server.list_events(time_min="2026-01-01T00:00:00Z", time_max="2026-01-31T00:00:00Z")
    assert results == [{"id": "e1"}]
    assert route.calls.last.request.url.params["startDateTime"] == "2026-01-01T00:00:00Z"


@respx.mock
async def test_search_events_uses_events_endpoint_not_calendar_view():
    route = respx.get(f"{server.ME}/events").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "e1"}],
    }))
    results = await server.search_events("standup")
    assert results == [{"id": "e1"}]
    assert route.calls.last.request.url.params["$search"] == '"standup"'


@respx.mock
async def test_list_events_non_primary_calendar_uses_calendar_id_path():
    route = respx.get(f"{server.ME}/calendars/cal2/calendarView").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    await server.list_events(calendar_id="cal2", time_min="2026-01-01T00:00:00Z",
                             time_max="2026-01-02T00:00:00Z")
    assert route.called


# ── retry behavior ────────────────────────────────────────────────────────

@respx.mock
async def test_send_email_retries_transient_5xx_and_recovers():
    route = respx.post(f"{server.ME}/sendMail").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(202),
    ])
    result = await server.send_email("a@example.com", "Subj", "Body")
    assert route.call_count == 2
    assert result == {"sent": True}


@respx.mock
async def test_send_email_reply_uses_native_reply_action():
    route = respx.post(f"{server.ME}/messages/orig1/reply").mock(return_value=httpx.Response(202))
    result = await server.send_email("a@example.com", "ignored-subject", "my reply",
                                     reply_to_message_id="orig1")
    assert result == {"sent": True, "replyTo": "orig1"}
    import json
    body = json.loads(route.calls.last.request.content)
    assert body["comment"] == "my reply"
