import pytest

from ravn.onboarding_client import ConnectBinding

ACTOR = ("support-app", "acme", "alice")


def test_original_browser_required_even_when_user_matches():
    binding = ConnectBinding.for_login(ACTOR, "verified-login-one")
    binding.session_id = "cs_one"
    query = {"session_id": "cs_one", "app_state": binding.app_state, "completion_code": "x" * 43}
    with pytest.raises(ValueError):
        binding.consume(ACTOR, "different-browser-same-user", query)
    with pytest.raises(ValueError):
        binding.consume(("support-app", "other-tenant", "alice"), "verified-login-one", query)
    assert binding.consume(ACTOR, "verified-login-one", query) == "x" * 43
    with pytest.raises(ValueError):
        binding.consume(ACTOR, "verified-login-one", query)


@pytest.mark.parametrize(
    "changed",
    [
        {"session_id": "cs_other"},
        {"app_state": "wrong"},
        {"app_state": "☃"},
        {"completion_code": None},
    ],
)
def test_return_must_match_original_transaction(changed):
    binding = ConnectBinding.for_login(ACTOR, "login")
    binding.session_id = "cs_one"
    query = {
        "session_id": "cs_one",
        "app_state": binding.app_state,
        "completion_code": "x" * 43,
        **changed,
    }
    with pytest.raises(ValueError):
        binding.consume(ACTOR, "login", query)
    assert not binding.consumed


def test_binding_deadline_and_secret_repr():
    binding = ConnectBinding.for_login(ACTOR, "login")
    binding.session_id = "cs_one"
    binding.expires = 0
    assert binding.app_state not in repr(binding)
    with pytest.raises(ValueError):
        binding.consume(
            ACTOR,
            "login",
            {"session_id": "cs_one", "app_state": binding.app_state, "completion_code": "x" * 43},
        )
