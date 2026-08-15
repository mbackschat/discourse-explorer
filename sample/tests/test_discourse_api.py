"""Tests for `sample.seed.discourse_api.DiscourseClient` (Sit 13).

Two suites:

* `DiscourseAPIUnitTests` — fast, hermetic. Mock `requests.Session.request`
  so we can verify header injection, payload shape, and error mapping
  without an actual Discourse instance.

* `DiscourseAPIIntegrationTests` — gated behind `SAMPLE_DISCOURSE_INTEGRATION`
  + a reachable `DISCOURSE_HOST`; default CI run skips them. They round-trip
  every entity type against the live `make -C sample up` stack.

Cleanup posture for integration: every entity created by a test gets a
unique name suffixed with the current epoch (e.g. `sample-cat-1714080000`)
so reruns don't collide. We deliberately do NOT delete on teardown — the
Discourse delete endpoints differ per entity type and the cost of a
slightly cluttered test forum is much lower than the cost of a flaky
cleanup. `make -C sample nuke` is the documented escape hatch.
"""

from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import requests

from sample.seed.discourse_api import (
    DiscourseAPIError,
    DiscourseClient,
    _to_iso,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    *, status: int = 200, json_body: object = None, text: str = ""
) -> MagicMock:
    """Build a `requests.Response`-shaped mock for a single call.

    `status` lands on `status_code`; `json_body` (if given) is what `.json()`
    returns; `text` is the body for the error path. `content` is set so the
    "empty body? skip parsing" branch in `_request` works as expected.
    """
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    body_text = text
    if json_body is not None and not text:
        import json as _json
        body_text = _json.dumps(json_body)
    resp.text = body_text
    resp.content = body_text.encode("utf-8") if body_text else b""
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no json body")
    return resp


def _make_client(**overrides) -> DiscourseClient:
    """Construct a client with stable defaults for unit tests."""
    kwargs = {
        "base_url": "https://discourse.example",
        "api_key": "test-key",
        "api_username": "admin",
    }
    kwargs.update(overrides)
    return DiscourseClient(**kwargs)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class DiscourseAPIUnitTests(unittest.TestCase):
    """Hermetic unit tests — no network, mock the session."""

    # -- constructor + headers ---------------------------------------------

    def test_constructor_strips_trailing_slash(self) -> None:
        client = _make_client(base_url="https://x.example/")
        self.assertEqual(client.base_url, "https://x.example")

    def test_constructor_rejects_empty_inputs(self) -> None:
        with self.assertRaises(ValueError):
            DiscourseClient(base_url="", api_key="k", api_username="u")
        with self.assertRaises(ValueError):
            DiscourseClient(base_url="https://x", api_key="", api_username="u")
        with self.assertRaises(ValueError):
            DiscourseClient(base_url="https://x", api_key="k", api_username="")

    def test_headers_include_api_key_and_username(self) -> None:
        client = _make_client()
        headers = client._headers()
        self.assertEqual(headers["Api-Key"], "test-key")
        self.assertEqual(headers["Api-Username"], "admin")
        self.assertIn("DiscourseExplorer", headers["User-Agent"])

    def test_headers_as_username_override(self) -> None:
        """`as_username` swaps `Api-Username` for the call without persisting."""
        client = _make_client()
        overridden = client._headers(as_username="alice")
        self.assertEqual(overridden["Api-Username"], "alice")
        # The default unchanged on the next call.
        self.assertEqual(client._headers()["Api-Username"], "admin")

    # -- create_category ---------------------------------------------------

    def test_create_category_posts_to_categories_json(self) -> None:
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(
                json_body={"category": {"id": 42, "name": "Bug Reports"}}
            ),
        ) as mocked:
            cat_id = client.create_category("Bug Reports")

        self.assertEqual(cat_id, 42)
        self.assertEqual(mocked.call_count, 1)
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://discourse.example/categories.json")
        self.assertEqual(kwargs["json"]["name"], "Bug Reports")
        # Default colors are set so Discourse renders them.
        self.assertEqual(kwargs["json"]["color"], "BF1E2E")
        self.assertEqual(kwargs["json"]["text_color"], "FFFFFF")
        self.assertEqual(kwargs["headers"]["Api-Key"], "test-key")
        self.assertEqual(kwargs["headers"]["Api-Username"], "admin")

    def test_create_category_with_description(self) -> None:
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"category": {"id": 7}}),
        ) as mocked:
            client.create_category("X", description="A test category")
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["json"]["description"], "A test category")

    def test_create_category_missing_id_raises(self) -> None:
        """Malformed Discourse responses surface as `DiscourseAPIError`."""
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"category": {}}),
        ):
            with self.assertRaises(DiscourseAPIError):
                client.create_category("X")

    # -- create_tag --------------------------------------------------------

    def test_create_tag_is_a_noop_returning_name(self) -> None:
        """Modern Discourse auto-creates tags; method returns input verbatim."""
        client = _make_client()
        with patch.object(client.session, "request") as mocked:
            result = client.create_tag("bug")
        self.assertEqual(result, "bug")
        # No HTTP call — the no-op contract is what the pipeline relies on
        # for cheap idempotency.
        mocked.assert_not_called()

    def test_create_tag_rejects_empty(self) -> None:
        client = _make_client()
        with self.assertRaises(ValueError):
            client.create_tag("")

    # -- create_user -------------------------------------------------------

    def test_create_user_payload_and_id_happy_path(self) -> None:
        """Older Discourse responses (or some config branches) include
        `user_id` directly — the client returns it without the follow-up
        GET. Pinning this keeps the happy path zero-overhead.
        """
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(
                json_body={"success": True, "user_id": 101, "active": True}
            ),
        ) as mocked:
            uid = client.create_user(
                "salty_gull",
                "secret-pw",
                "salty_gull@example.com",
                name="Salty Gull",
            )
        self.assertEqual(uid, 101)
        # Single request — no follow-up GET when the POST already carries id.
        self.assertEqual(mocked.call_count, 1)
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["json"]["username"], "salty_gull")
        self.assertEqual(kwargs["json"]["email"], "salty_gull@example.com")
        self.assertEqual(kwargs["json"]["password"], "secret-pw")
        self.assertEqual(kwargs["json"]["name"], "Salty Gull")
        # active + approved default True so the new user can post immediately.
        self.assertTrue(kwargs["json"]["active"])
        self.assertTrue(kwargs["json"]["approved"])

    def test_create_user_two_step_lookup_when_user_id_missing(self) -> None:
        """Modern Discourse (admin-created + auto-activated) returns
        `{success, active, message}` with NO `user_id`. The client must
        fall back to `GET /u/<username>.json` to fetch the canonical id.
        """
        client = _make_client()
        post_response = _mock_response(
            json_body={
                "success": True,
                "active": True,
                "message": "Your account is activated and ready to use.",
            }
        )
        get_response = _mock_response(
            json_body={"user": {"id": 42, "username": "salty_gull"}}
        )
        with patch.object(
            client.session,
            "request",
            side_effect=[post_response, get_response],
        ) as mocked:
            uid = client.create_user(
                "salty_gull", "secret-pw", "salty_gull@example.com"
            )
        self.assertEqual(uid, 42)
        # Both requests fired in order: POST /users.json then GET /u/<u>.json.
        self.assertEqual(mocked.call_count, 2)
        first_call, second_call = mocked.call_args_list
        self.assertEqual(first_call.args[0], "POST")
        self.assertEqual(
            first_call.args[1], "https://discourse.example/users.json"
        )
        self.assertEqual(second_call.args[0], "GET")
        self.assertEqual(
            second_call.args[1],
            "https://discourse.example/u/salty_gull.json",
        )
        # The follow-up GET reuses the same auth headers as the POST.
        self.assertEqual(
            second_call.kwargs["headers"]["Api-Key"], "test-key"
        )

    def test_create_user_raises_when_success_false(self) -> None:
        """`success: false` must raise rather than fall through to the GET
        (otherwise we'd look up a user that does not exist and either 404
        or — worse — return some other user that happens to share the
        username slot).
        """
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(
                json_body={
                    "success": False,
                    "message": "Username must be unique",
                }
            ),
        ) as mocked:
            with self.assertRaises(DiscourseAPIError) as ctx:
                client.create_user("dup", "pw", "dup@example.com")
        self.assertIn("Username must be unique", ctx.exception.body)
        # Should NOT have fallen through to a follow-up GET.
        self.assertEqual(mocked.call_count, 1)

    def test_create_user_raises_when_lookup_lacks_user_id(self) -> None:
        """If the follow-up GET also fails to surface a numeric id, that's
        a real malformed-response case — surface it via DiscourseAPIError.
        """
        client = _make_client()
        post_response = _mock_response(
            json_body={"success": True, "active": True, "message": "ok"}
        )
        get_response = _mock_response(json_body={"user": {}})
        with patch.object(
            client.session,
            "request",
            side_effect=[post_response, get_response],
        ):
            with self.assertRaises(DiscourseAPIError) as ctx:
                client.create_user("u", "pw", "u@example.com")
        self.assertIn("missing user.id", ctx.exception.body)

    def test_create_user_name_defaults_to_username(self) -> None:
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(
                json_body={"success": True, "user_id": 1}
            ),
        ) as mocked:
            client.create_user("u", "p", "u@x.com")
        self.assertEqual(mocked.call_args.kwargs["json"]["name"], "u")

    # -- set_site_setting --------------------------------------------------

    def test_set_site_setting_puts_to_admin_endpoint(self) -> None:
        """`set_site_setting(name, value)` PUTs to `/admin/site_settings/<name>`."""
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={}, status=200),
        ) as mocked:
            client.set_site_setting("rate_limit_create_post", 1)
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], "PUT")
        self.assertEqual(
            args[1],
            "https://discourse.example/admin/site_settings/rate_limit_create_post.json",
        )
        # Payload uses the setting name as the key.
        self.assertEqual(kwargs["json"], {"rate_limit_create_post": 1})

    def test_set_site_setting_stringifies_booleans(self) -> None:
        """Discourse expects bool settings as `"true"` / `"false"` strings."""
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={}, status=200),
        ) as mocked:
            client.set_site_setting("login_required", False)
        self.assertEqual(
            mocked.call_args.kwargs["json"], {"login_required": "false"}
        )

    def test_set_site_setting_rejects_empty_name(self) -> None:
        client = _make_client()
        with self.assertRaises(ValueError):
            client.set_site_setting("", 1)

    # -- create_topic ------------------------------------------------------

    def test_create_topic_posts_to_posts_json(self) -> None:
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(
                json_body={"id": 555, "topic_id": 99, "post_number": 1}
            ),
        ) as mocked:
            result = client.create_topic(
                "Hello forum",
                "First post body",
                category_id=3,
                tags=["bug", "remaster"],
            )
        self.assertEqual(result["topic_id"], 99)
        self.assertEqual(result["id"], 555)
        args, kwargs = mocked.call_args
        # Topics go through /posts.json — not /topics.json — see module docstring.
        self.assertEqual(args[1], "https://discourse.example/posts.json")
        self.assertEqual(kwargs["json"]["title"], "Hello forum")
        self.assertEqual(kwargs["json"]["raw"], "First post body")
        self.assertEqual(kwargs["json"]["category"], 3)
        self.assertEqual(kwargs["json"]["tags"], ["bug", "remaster"])
        # No created_at and no author_username were given, so neither the
        # payload nor the header should mention them.
        self.assertNotIn("created_at", kwargs["json"])
        self.assertEqual(kwargs["headers"]["Api-Username"], "admin")

    def test_create_topic_includes_iso_created_at(self) -> None:
        """`created_at` lands in the payload as an ISO 8601 UTC string."""
        client = _make_client()
        ts = datetime(2025, 4, 26, 10, 30, 0, tzinfo=timezone.utc)
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"id": 1, "topic_id": 1}),
        ) as mocked:
            client.create_topic(
                "T", "body", category_id=1, created_at=ts
            )
        payload = mocked.call_args.kwargs["json"]
        self.assertEqual(payload["created_at"], "2025-04-26T10:30:00Z")

    def test_create_topic_naive_datetime_treated_as_utc(self) -> None:
        client = _make_client()
        ts = datetime(2025, 4, 26, 10, 30, 0)  # naive → UTC by convention
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"id": 1, "topic_id": 1}),
        ) as mocked:
            client.create_topic("T", "body", category_id=1, created_at=ts)
        self.assertEqual(
            mocked.call_args.kwargs["json"]["created_at"],
            "2025-04-26T10:30:00Z",
        )

    def test_create_topic_with_author_username_swaps_header(self) -> None:
        """`author_username` overrides `Api-Username` for this one call."""
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"id": 1, "topic_id": 1}),
        ) as mocked:
            client.create_topic(
                "T", "body", category_id=1, author_username="salty_gull"
            )
        self.assertEqual(
            mocked.call_args.kwargs["headers"]["Api-Username"], "salty_gull"
        )
        # Default `api_username` is unchanged on the client itself.
        self.assertEqual(client.api_username, "admin")

    # -- create_post -------------------------------------------------------

    def test_create_post_basic(self) -> None:
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(
                json_body={"id": 9001, "topic_id": 99, "post_number": 5}
            ),
        ) as mocked:
            result = client.create_post(99, "reply body")
        self.assertEqual(result["id"], 9001)
        self.assertEqual(result["post_number"], 5)
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["json"]["topic_id"], 99)
        self.assertEqual(kwargs["json"]["raw"], "reply body")
        # No reply_to_post_number → key absent (so it defaults to OP).
        self.assertNotIn("reply_to_post_number", kwargs["json"])

    def test_create_post_reply_to_uses_post_number_not_id(self) -> None:
        """`reply_to_post_number` is the *post_number*, not the post `id`.

        Documented in `docs/discourse/DISCOURSE_JSON_TERMINOLOGY.md`. The
        client just forwards the int — this test pins the field name.
        """
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"id": 1}),
        ) as mocked:
            client.create_post(99, "reply", reply_to_post_number=3)
        self.assertEqual(
            mocked.call_args.kwargs["json"]["reply_to_post_number"], 3
        )

    def test_create_post_as_username_swaps_header(self) -> None:
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"id": 1}),
        ) as mocked:
            client.create_post(
                99, "reply", author_username="alice"
            )
        self.assertEqual(
            mocked.call_args.kwargs["headers"]["Api-Username"], "alice"
        )

    def test_create_post_includes_iso_created_at(self) -> None:
        client = _make_client()
        ts = datetime(2025, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(json_body={"id": 1}),
        ) as mocked:
            client.create_post(99, "reply", created_at=ts)
        self.assertEqual(
            mocked.call_args.kwargs["json"]["created_at"],
            "2025-04-27T12:00:00Z",
        )

    # -- error path --------------------------------------------------------

    def test_non_2xx_raises_with_url_status_body(self) -> None:
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            return_value=_mock_response(
                status=422,
                text='{"errors":["Title has already been used"]}',
            ),
        ):
            with self.assertRaises(DiscourseAPIError) as ctx:
                client.create_topic("dup", "x", category_id=1)
        err = ctx.exception
        self.assertEqual(err.status, 422)
        self.assertEqual(
            err.url, "https://discourse.example/posts.json"
        )
        self.assertIn("Title has already been used", err.body)

    def test_transport_error_wrapped_as_discourse_api_error(self) -> None:
        """Connection failure is surfaced via `DiscourseAPIError(status=0, ...)`."""
        client = _make_client()
        with patch.object(
            client.session,
            "request",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            with self.assertRaises(DiscourseAPIError) as ctx:
                client.create_category("X")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("transport error", ctx.exception.body)

    # -- retry config ------------------------------------------------------

    def test_session_has_retry_adapter_for_429_and_5xx(self) -> None:
        """The mounted adapter retries on 429 + 5xx with backoff factor 0.5.

        Asserts the retry config matches `discourse_explorer/scraper.py`'s
        conventions — a maintainer changing one is signalled to update both.
        We inspect the adapter's `max_retries` because `requests.Session` does
        not expose retries any other way.
        """
        client = _make_client()
        adapter = client.session.get_adapter("https://discourse.example")
        retry = adapter.max_retries
        # urllib3 stores `total` on the Retry object.
        self.assertEqual(retry.total, 3)
        self.assertEqual(retry.backoff_factor, 0.5)
        for code in (429, 500, 502, 503, 504):
            self.assertIn(code, retry.status_forcelist)
        # POST must be retryable — it's the only verb the create-side uses.
        self.assertIn("POST", retry.allowed_methods)

    def test_session_disables_keep_alive(self) -> None:
        """Session-level `Connection: close` header — Sit 14.1 fix.

        Discourse FIN'd long-running POSTs mid-response during the Sit-14
        verification, leaving reused keep-alive sockets in CLOSE_WAIT and
        wedging subsequent reads. A bulk push doesn't benefit from
        connection reuse anyway (Sidekiq dwarfs TCP-handshake savings), so
        we force every request to open a fresh socket.
        """
        client = _make_client()
        self.assertEqual(client.session.headers.get("Connection"), "close")

    def test_default_timeout_is_tuple(self) -> None:
        """`timeout` is the (connect, read) tuple form — Sit 14.1 fix.

        The float form (`timeout=30.0`) was observed to NOT release a
        half-closed-keepalive hang in the Sit-14 verification despite the
        configured 30s. The tuple form passes the read deadline through to
        urllib3 cleanly so the Retry adapter sees a real timeout.
        """
        client = _make_client()
        self.assertEqual(client.timeout, (5.0, 30.0))


class IsoFormatTests(unittest.TestCase):
    """Targeted tests for `_to_iso` — single source of truth for `created_at`."""

    def test_naive_datetime_treated_as_utc(self) -> None:
        ts = datetime(2025, 4, 26, 10, 30, 0)
        self.assertEqual(_to_iso(ts), "2025-04-26T10:30:00Z")

    def test_aware_datetime_converted_to_utc(self) -> None:
        # Construct a non-UTC datetime via a fixed offset; result must shift.
        from datetime import timedelta
        plus_two = timezone(timedelta(hours=2))
        ts = datetime(2025, 4, 26, 12, 30, 0, tzinfo=plus_two)
        # 12:30 +02:00 → 10:30 UTC.
        self.assertEqual(_to_iso(ts), "2025-04-26T10:30:00Z")

    def test_seconds_precision(self) -> None:
        """Sub-second precision is dropped — Discourse's UI is second-level."""
        ts = datetime(2025, 4, 26, 10, 30, 5, microsecond=123456)
        self.assertEqual(_to_iso(ts), "2025-04-26T10:30:05Z")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def _integration_enabled() -> bool:
    """Both the opt-in flag AND a reachable host are required.

    The flag alone isn't sufficient — without a live forum the round-trip
    tests are guaranteed to fail with a confusing connection error. Doubly
    gating means CI without the stack stays green by skipping rather than
    erroring.
    """
    if not os.environ.get("SAMPLE_DISCOURSE_INTEGRATION"):
        return False
    host = os.environ.get("DISCOURSE_HOST")
    if not host:
        return False
    # We only sanity-check that the URL parses; a real reachability probe
    # would require a network call from `setUpClass`, which is awkward when
    # the suite is skipped anyway. Failures during the actual tests give
    # clearer diagnostics than a setup-time probe would.
    parsed = urlparse(host if "://" in host else f"http://{host}")
    return bool(parsed.netloc)


@unittest.skipUnless(
    _integration_enabled(),
    "Discourse integration test — set SAMPLE_DISCOURSE_INTEGRATION=1 + DISCOURSE_HOST to opt in",
)
class DiscourseAPIIntegrationTests(unittest.TestCase):
    """Live round-trip against the `make -C sample up` Discourse stack.

    Skipped by default. Each entity is named with an epoch suffix to avoid
    collisions across reruns; we deliberately do not delete them — see
    module docstring for rationale.
    """

    @classmethod
    def setUpClass(cls) -> None:
        host = os.environ["DISCOURSE_HOST"]
        if "://" not in host:
            host = f"http://{host}"
        api_key = os.environ["DISCOURSE_API_KEY"]
        api_username = os.environ.get("DISCOURSE_API_USERNAME", "admin")
        cls.client = DiscourseClient(
            base_url=host,
            api_key=api_key,
            api_username=api_username,
            timeout=(5.0, 30.0),
        )
        cls.run_suffix = str(int(time.time()))

    def _suffix(self, base: str) -> str:
        return f"{base}-{self.run_suffix}"

    def test_create_category_round_trip(self) -> None:
        name = self._suffix("sample-cat")
        cat_id = self.client.create_category(name)
        self.assertGreater(cat_id, 0)
        # Read back via the same session — confirms name persisted.
        result = self.client._request("GET", f"/c/{cat_id}/show.json")
        self.assertEqual(result["category"]["name"], name)

    def test_create_user_round_trip(self) -> None:
        username = self._suffix("seeduser")
        # Discourse usernames have length and char-set rules; epoch suffix is
        # digits only, so `seeduser-<epoch>` is valid (Discourse allows `-`).
        email = f"{username}@example.com"
        uid = self.client.create_user(
            username, "supersecretpw-1234", email, name=username.title()
        )
        self.assertGreater(uid, 0)
        result = self.client._request("GET", f"/u/{username}.json")
        self.assertEqual(result["user"]["username"], username)
        # Discourse may not echo the email back unless you're admin viewing
        # your own profile — accept either an exact match or a missing field.
        echoed = result["user"].get("email")
        if echoed:
            self.assertEqual(echoed, email)

    def test_create_topic_with_backdate_and_tags(self) -> None:
        # Need a category to host the topic.
        cat_id = self.client.create_category(self._suffix("topic-host"))
        title = self._suffix("Backdated topic")
        body = "Body for backdated topic — integration check."
        tags = [self._suffix("itag")]
        backdate = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)

        result = self.client.create_topic(
            title, body, category_id=cat_id, tags=tags, created_at=backdate
        )
        topic_id = result["topic_id"]
        # Fetch and verify backdate + tag set + body.
        topic = self.client._request("GET", f"/t/{topic_id}.json")
        self.assertEqual(topic["title"], title)
        self.assertIn(tags[0], topic.get("tags", []))
        # `created_at` round-trips as ISO; just check the date portion to
        # avoid timezone-format flakiness.
        self.assertTrue(topic["created_at"].startswith("2024-01-15"))
        # Body lives on the OP post. `/t/<id>.json` returns the rendered
        # `cooked` HTML, NOT the source `raw` — the parent project's
        # scraper.py reads `cooked` (and html-to-texts it) for exactly this
        # reason. To get `raw` you'd hit `/posts/<id>.json` separately. We
        # assert against `cooked` to match the real scraper-side surface.
        op = topic["post_stream"]["posts"][0]
        self.assertIn("backdated topic", op["cooked"].lower())

    def test_end_to_end_live_init(self) -> None:
        """Sit 14 end-to-end smoke: bake a tiny forum + push, then verify.

        Runs `pipeline.push_forum` against the live stack with the smallest
        scale, then re-fetches counts via the same client to confirm the
        push actually landed. Re-run hygiene: this test uses a per-run
        epoch-derived seed so each invocation produces a disjoint forum
        (different usernames + titles), avoiding 422 collisions on rerun.
        We deliberately do NOT clear the forum first; cleanup is via
        `make -C sample nuke`.

        Test duration: tiny scale takes ~3-5 minutes against a live stack
        because of per-user `rate_limit_create_post` gaps (Pareto activity
        weights mean a few "heavy poster" users author many adjacent posts,
        and each adjacent same-user post has to wait ~6.5s to clear the
        Discourse default).
        """
        # Lazy-import so the dry-run unit suite doesn't have to load the
        # whole pipeline + every generator.
        from sample.seed.pipeline import push_forum
        from sample.seed.product import crown_of_brine
        from sample.seed.universe import GenerationSpec

        # Use the per-run epoch suffix in the seed so reruns produce a
        # disjoint forum and don't collide with prior test runs. Pareto
        # weights and structural counts are scale-driven; a different seed
        # just shuffles names, which is what we want for re-run hygiene.
        seed = int(time.time()) % 100_000
        spec = GenerationSpec(
            seed=seed, scale="tiny", product=crown_of_brine
        )
        # Use the existing build_forum with no body provider — placeholder
        # bodies are ≤25 chars which fail Discourse's default
        # `min_post_length=20`. We pad them to clear that floor.
        from sample.seed.pipeline import build_forum

        def _padded_body_provider(topic, post):  # noqa: ARG001
            return (
                f"placeholder body for topic {post.topic_id} "
                f"post {post.post_number} — padded to clear min_post_length."
            )

        forum = build_forum(
            spec,
            body_provider=_padded_body_provider,
            product_name="crown-of-brine",
        )

        result = push_forum(forum, self.client)

        # Sanity checks on the result shape.
        self.assertEqual(
            len(result.category_ids), len(forum.categories)
        )
        self.assertEqual(len(result.user_ids), len(forum.users))
        self.assertEqual(len(result.topic_ids), len(forum.topics))
        # post_count includes both OPs and replies.
        self.assertGreaterEqual(result.post_count, len(forum.topics))
        # Re-fetch one topic via the API to confirm it landed with the
        # correct title + body (cooked).
        sample_seeded_topic_id = forum.topics[0].id
        discourse_topic_id = result.topic_ids[sample_seeded_topic_id]
        topic_payload = self.client._request(
            "GET", f"/t/{discourse_topic_id}.json"
        )
        self.assertEqual(topic_payload["title"], forum.topics[0].title)
        # The OP body should appear in the cooked HTML.
        op = topic_payload["post_stream"]["posts"][0]
        self.assertIn(
            f"topic {sample_seeded_topic_id}", op["cooked"].lower()
        )

    def test_create_reply_with_as_username(self) -> None:
        """Reply authored by a freshly-created regular user."""
        cat_id = self.client.create_category(self._suffix("reply-host"))
        # Author #1: the OP, default admin.
        # Discourse's default `min_post_length` site setting is 20 characters
        # (applies to BOTH the topic OP and any reply body). We deliberately
        # do NOT lower this on the live forum — keeping the default means
        # the parent project's scraper meets the same validation surface in
        # this test as it would in the wild. Future maintainers: don't
        # shorten these strings without first lowering `min_post_length` on
        # the test stack, and don't lower `min_post_length`.
        result = self.client.create_topic(
            self._suffix("Reply target"),
            "OP body for the reply round-trip integration check.",
            category_id=cat_id,
        )
        topic_id = result["topic_id"]
        op_post_number = result["post_number"]

        # Create a regular user and reply as them.
        replier = self._suffix("replier")
        self.client.create_user(
            replier, "supersecretpw-1234", f"{replier}@example.com"
        )
        reply = self.client.create_post(
            topic_id,
            "I disagree with the OP — here is a longer reply body to clear min_post_length.",
            reply_to_post_number=op_post_number,
            author_username=replier,
        )
        self.assertGreater(reply["id"], 0)

        # Verify the reply lists the impersonated user as author.
        topic = self.client._request("GET", f"/t/{topic_id}.json")
        replies = [
            p
            for p in topic["post_stream"]["posts"]
            if p.get("post_number", 0) > 1
        ]
        self.assertTrue(replies, "no replies found on topic")
        last = replies[-1]
        self.assertEqual(last["username"], replier)
        self.assertEqual(last.get("reply_to_post_number"), op_post_number)


if __name__ == "__main__":
    unittest.main()
