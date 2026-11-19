"""PEM signing/verification and the signed handshake flow."""

import pytest

from seestar_controller.link import auth
from seestar_controller.link.auth import HandshakeError


def test_sign_and_verify_roundtrip(keypair):
    private_pem, public_pem = keypair
    private_key = auth.load_private_key(private_pem)
    public_key = auth.load_public_key(public_pem)
    challenge = "mock-challenge-0123456789abcdef"
    signature = auth.sign_challenge(private_key, challenge)
    assert auth.verify_challenge(public_key, challenge, signature)


def test_verify_rejects_tampered_challenge(keypair):
    private_pem, public_pem = keypair
    private_key = auth.load_private_key(private_pem)
    public_key = auth.load_public_key(public_pem)
    signature = auth.sign_challenge(private_key, "real challenge")
    assert not auth.verify_challenge(public_key, "forged challenge", signature)


def test_verify_rejects_wrong_key(keypair):
    private_pem, _public_pem = keypair
    _other_private, other_public = auth.generate_keypair()
    private_key = auth.load_private_key(private_pem)
    other_public_key = auth.load_public_key(other_public)
    signature = auth.sign_challenge(private_key, "challenge")
    assert not auth.verify_challenge(other_public_key, "challenge", signature)


def test_verify_rejects_garbage_signature(keypair):
    _private_pem, public_pem = keypair
    public_key = auth.load_public_key(public_pem)
    assert not auth.verify_challenge(public_key, "challenge", "!!!not-base64!!!")


def test_signed_handshake_success_9_75_style(keypair):
    """verify_client reply with key_envelope + code 0 counts as success (fw 9.75)."""
    private_pem, _public_pem = keypair
    private_key = auth.load_private_key(private_pem)
    challenge = "abc123"
    replies = {
        auth.AUTH_CHALLENGE_METHOD: {"result": {"str": challenge}, "code": 0},
        auth.AUTH_VERIFY_METHOD: {
            "result": {"key_envelope": {"wrapped": "x"}},
            "code": 0,
        },
    }
    seen = []

    def fake_request(method, params=None):
        seen.append((method, params))
        return replies[method]

    result = auth.signed_handshake(fake_request, private_key)
    assert result["code"] == 0
    # challenge requested first, then verify_client with sign + data
    assert seen[0] == (auth.AUTH_CHALLENGE_METHOD, None)
    method, params = seen[1]
    assert method == auth.AUTH_VERIFY_METHOD
    assert params["data"] == challenge
    assert auth.verify_challenge(
        auth.load_public_key(keypair[1]), challenge, params["sign"]
    )


def test_signed_handshake_success_legacy_result_zero(keypair):
    private_pem, _public_pem = keypair
    private_key = auth.load_private_key(private_pem)
    challenge = "legacy-challenge"

    def fake_request(method, params=None):
        if method == auth.AUTH_CHALLENGE_METHOD:
            return {"result": {"str": challenge}, "code": 0}
        return {"result": 0, "code": 0}

    assert auth.signed_handshake(fake_request, private_key)["result"] == 0


def test_signed_handshake_rejected():
    private_key = auth.load_private_key(auth.generate_keypair()[0])

    def fake_request(method, params=None):
        if method == auth.AUTH_CHALLENGE_METHOD:
            return {"result": {"str": "challenge"}, "code": 0}
        return {"result": {"error": "bad signature"}, "code": -1}

    with pytest.raises(HandshakeError):
        auth.signed_handshake(fake_request, private_key)


def test_signed_handshake_no_challenge():
    private_key = auth.load_private_key(auth.generate_keypair()[0])

    def fake_request(method, params=None):
        return {"result": {}, "code": 0}

    with pytest.raises(HandshakeError):
        auth.signed_handshake(fake_request, private_key)
