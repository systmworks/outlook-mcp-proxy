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
async def test_call_list_all_follows_nextlink_until_exhausted():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={
                "value": [{"id": "a"}], "@odata.nextLink": f"{server.ME}/next",
            })
        return httpx.Response(200, json={"value": [{"id": "b"}]})

    respx.get(f"{server.ME}/start").mock(side_effect=handler)
    respx.get(f"{server.ME}/next").mock(side_effect=handler)
    items = await server._call_list_all("GET", f"{server.ME}/start", params={"$top": 100})
    assert [i["id"] for i in items] == ["a", "b"]
    assert calls["n"] == 2


@respx.mock
async def test_call_list_all_stops_at_page_cap_on_never_ending_nextlink(monkeypatch):
    # Regression test: a self-referential or never-terminating @odata.nextLink
    # (e.g. from a misbehaving proxy/cache) must not hang a tool call forever.
    monkeypatch.setattr(server, "_MAX_PAGINATION_PAGES", 3)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={
            "value": [{"id": str(calls["n"])}], "@odata.nextLink": f"{server.ME}/loop",
        })

    respx.get(f"{server.ME}/loop").mock(side_effect=handler)
    items = await server._call_list_all("GET", f"{server.ME}/loop")
    assert calls["n"] == 3
    assert len(items) == 3


@respx.mock
async def test_call_list_all_resends_headers_on_every_page():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        assert request.headers["prefer"] == 'outlook.timezone="UTC"'
        if calls["n"] == 1:
            return httpx.Response(200, json={
                "value": [{"id": "a"}], "@odata.nextLink": f"{server.ME}/next2",
            })
        return httpx.Response(200, json={"value": [{"id": "b"}]})

    respx.get(f"{server.ME}/start2").mock(side_effect=handler)
    respx.get(f"{server.ME}/next2").mock(side_effect=handler)
    items = await server._call_list_all("GET", f"{server.ME}/start2",
                                        headers={"Prefer": 'outlook.timezone="UTC"'})
    assert [i["id"] for i in items] == ["a", "b"]
    assert calls["n"] == 2


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
async def test_read_conversation_escapes_embedded_quote_in_conversation_id():
    # Regression test: an unescaped "'" previously let a crafted conversation_id
    # break out of the $filter string literal and alter the query Graph received.
    route = respx.get(f"{server.ME}/messages").mock(return_value=httpx.Response(200, json={"value": []}))
    await server.read_conversation("x' or true or conversationId eq 'y")
    sent = route.calls.last.request.url.params["$filter"]
    assert sent == "conversationId eq 'x'' or true or conversationId eq ''y'"


@respx.mock
async def test_search_emails_escapes_embedded_quote_in_query():
    # Regression test: live-confirmed that an unescaped '"' let a crafted query
    # break out of the intended $search phrase (garbage term + '" OR "a'
    # returned unrelated messages instead of zero results).
    route = respx.get(f"{server.ME}/messages").mock(return_value=httpx.Response(200, json={"value": []}))
    await server.search_emails('zzz" OR "a')
    sent = route.calls.last.request.url.params["$search"]
    assert sent == '"zzz OR a"'


@respx.mock
async def test_read_message_url_encodes_message_id_containing_slash():
    # Regression test: Graph message ids are documented to sometimes contain
    # '/' — a reserved path delimiter — which must be percent-encoded or it
    # splits the request onto an unintended path.
    route = respx.get(url__regex=r".*/messages/.*").mock(return_value=httpx.Response(200, json={
        "id": "m1/weird", "conversationId": "", "from": None, "toRecipients": [],
        "ccRecipients": [], "subject": "", "receivedDateTime": "", "bodyPreview": "",
        "body": {"contentType": "text", "content": ""}, "categories": [],
        "parentFolderId": "", "hasAttachments": False,
    }))
    await server.read_message("m1/weird")
    assert route.calls.last.request.url.raw_path == \
        b"/v1.0/me/messages/m1%2Fweird"


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


@respx.mock
async def test_get_attachment_rejects_missing_content_bytes():
    # Regression test: a referenceAttachment (e.g. a OneDrive link) or certain
    # itemAttachment types carry no contentBytes — treating that as a valid
    # empty download would silently hand back a 0-byte "success" instead of a
    # clear error that this attachment type isn't downloadable this way.
    respx.get(f"{server.ME}/messages/m1/attachments/a1").mock(return_value=httpx.Response(
        200, json={"name": "link.url", "contentType": "text/plain", "size": 0},
    ))
    with pytest.raises(ValueError, match="no downloadable content"):
        await server.get_attachment("m1", "a1")


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
    ("move_folder", ("f1", "inbox")),
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
async def test_list_folders_follows_pagination_beyond_one_page():
    # Regression test: _list_child_folders previously returned only the first
    # page of Graph's response (observed in production: exactly 100 items)
    # with no indication to the caller that more folders existed.
    # nextLink targets a distinct path (not /mailFolders again) so respx's
    # query-string-agnostic route matching can't accidentally loop forever by
    # matching the first page's route again.
    respx.get(f"{server.ME}/mailFolders").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "a", "displayName": "A", "childFolderCount": 0}],
        "@odata.nextLink": f"{server.ME}/mailFolders_page2",
    }))
    respx.get(f"{server.ME}/mailFolders_page2").mock(return_value=httpx.Response(200, json={
        "value": [{"id": "b", "displayName": "B", "childFolderCount": 0}],
    }))
    folders = await server.list_folders()
    assert {f["id"] for f in folders} == {"a", "b"}


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
async def test_move_folder_reparents_under_destination():
    route = respx.post(f"{server.ME}/mailFolders/f1/move").mock(return_value=httpx.Response(200, json={
        "id": "f1", "displayName": "Lot 33", "parentFolderId": "inbox-id",
    }))
    result = await server.move_folder("f1", "inbox")
    import json
    body = json.loads(route.calls.last.request.content)
    assert body["destinationId"] == "inbox"
    assert result["parentFolderId"] == "inbox-id"


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
