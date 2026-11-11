"""Arena rate limits are per account, not per IP (venue NAT safety)."""

import pytest

from tests.arena.helpers import create_arena_ctfd, gen_event, make_user
from tests.helpers import destroy_ctfd, login_as_user

API = "/api/v1/arena"


@pytest.fixture
def app():
    app = create_arena_ctfd()
    app.config["RATELIMIT_LOGIN"] = (1000, 5)
    yield app
    destroy_ctfd(app)


def _register(client, eid):
    return client.post(f"{API}/events/{eid}/register", json={})


def test_limit_is_per_account_behind_one_ip(app):
    with app.app_context():
        eid = gen_event("rl", registration_open=True).id
        make_user("alice")
        make_user("bob")
    alice = login_as_user(app, name="alice")
    bob = login_as_user(app, name="bob")  # same test-client IP as alice
    codes = [_register(alice, eid).status_code for _ in range(10)]
    assert 429 not in codes
    r = _register(alice, eid)
    assert r.status_code == 429 and r.get_json()["code"] == "ratelimited"
    # An IP-keyed limiter would now block bob too.
    assert _register(bob, eid).status_code != 429


def test_anonymous_requests_fall_back_to_ip(app):
    from CTFd.plugins.arena.core.ratelimit import ratelimit

    calls = []

    @ratelimit(method="GET", limit=2, interval=60, key_prefix="t-anon")
    def view():
        calls.append(1)
        return "ok"

    with app.test_request_context("/x", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert view() == "ok" and view() == "ok"
        from CTFd.plugins.arena.api import ApiError

        with pytest.raises(ApiError) as e:
            view()
        assert e.value.status == 429
    with app.test_request_context("/x", environ_base={"REMOTE_ADDR": "10.0.0.2"}):
        assert view() == "ok"  # different anonymous IP


def test_ratelimit_disable_is_honoured(app):
    app.config["RATELIMIT_DISABLE"] = True
    with app.app_context():
        eid = gen_event("rl2", registration_open=True).id
        make_user("carol")
    c = login_as_user(app, name="carol")
    assert all(_register(c, eid).status_code != 429 for _ in range(15))
