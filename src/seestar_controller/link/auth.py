"""PEM-signed handshake for the Link (firmware >= 7.18, incl. 9.75).

Firmware 7.18+ gates the command channel behind a challenge-response
handshake, as implemented by the open-source seestar_alp / seestarpy
projects:

1. client sends ``{"id": N, "method": "get_verify_str"}`` and the Scope
   replies ``result: {"str": "<challenge>"}``;
2. the client signs the challenge string with its RSA private key
   (RSASSA-PKCS1-v1_5 + SHA-1, base64-encoded) and sends
   ``{"id": N, "method": "verify_client",
       "params": {"sign": <b64 signature>, "data": <challenge>}}``;
3. the Scope replies ``result: 0`` (older firmware) or, on firmware
   9.75+, ``result: {"key_envelope": {...}}`` with ``code: 0``;
4. optionally the client confirms with ``pi_is_verified`` -> ``result: true``.

The private key is the "interop PEM" extracted once from the official
Android app (seestarpy's ``extract_pem``) and stored at
``~/.config/seestar-controller/key.pem``; the path is configurable.
Loading, saving (0600), and extraction live in
:mod:`seestar_controller.link.keys`; this module only signs/verifies.

NOTE: the sign/verify handshake was live-verified against a real Seestar
S50 (station mode, firmware 9.75, 2026-11-20) with the extracted PEM key;
the round-trip is additionally tested with generated *test* keys against
the mock Scope.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

#: Default location of the extracted interop PEM key.
DEFAULT_KEY_PATH = Path.home() / ".config" / "seestar-controller" / "key.pem"

#: RPC methods that make up the signed handshake.
AUTH_CHALLENGE_METHOD = "get_verify_str"
AUTH_VERIFY_METHOD = "verify_client"
AUTH_STATUS_METHOD = "pi_is_verified"


class HandshakeError(Exception):
    """Raised when the signed handshake fails."""


def generate_keypair(key_size: int = 2048) -> tuple[bytes, bytes]:
    """Generate a fresh RSA keypair as (private_pem, public_pem) bytes.

    Used for tests and for the standalone mock Scope; the real key comes
    from the official app, not from here.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def load_private_key(pem: bytes | str | Path) -> Any:
    """Load a PEM private key from bytes or a path."""
    if isinstance(pem, (str, Path)):
        pem = Path(pem).read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def load_public_key(pem: bytes | str | Path) -> Any:
    """Load a PEM public key from bytes or a path."""
    if isinstance(pem, (str, Path)):
        pem = Path(pem).read_bytes()
    return serialization.load_pem_public_key(pem)


def sign_challenge(private_key: Any, challenge: str) -> str:
    """Sign the challenge string (RSASSA-PKCS1-v1_5 + SHA-1), base64 out."""
    signature = private_key.sign(
        challenge.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1()
    )
    return base64.b64encode(signature).decode("ascii")


def verify_challenge(public_key: Any, challenge: str, signature_b64: str) -> bool:
    """Verify a base64 RSA-SHA1 signature over the challenge string."""
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(
            signature, challenge.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1()
        )
        return True
    except Exception:
        return False


def signed_handshake(
    request: Callable[[str, Mapping[str, Any] | None], Any],
    private_key: Any,
) -> dict[str, Any]:
    """Run the firmware >= 7.18 signed handshake over an open Link.

    ``request`` sends one RPC and returns the reply — either a raw dict or a
    :class:`~seestar_controller.link.protocol.Reply` (e.g.
    ``LinkTransport.request_raw``). Returns the ``verify_client`` reply on
    success; raises :class:`HandshakeError` otherwise.
    """
    challenge_reply = request(AUTH_CHALLENGE_METHOD, None)
    challenge_reply = getattr(challenge_reply, "raw", challenge_reply)
    challenge = (challenge_reply.get("result") or {}).get("str", "")
    if not challenge:
        raise HandshakeError(
            f"scope returned no auth challenge: {challenge_reply!r}"
        )

    signature = sign_challenge(private_key, challenge)
    verify_reply = request(
        AUTH_VERIFY_METHOD, {"sign": signature, "data": challenge}
    )
    verify_reply = getattr(verify_reply, "raw", verify_reply)
    result = verify_reply.get("result", verify_reply.get("code", -1))
    code = verify_reply.get("code", 0)
    # Older firmware: result == 0. Firmware 9.75+: result is a dict with a
    # key_envelope and success is signalled by code == 0.
    ok = result == 0 or (
        isinstance(result, dict) and "key_envelope" in result and code == 0
    )
    if not ok:
        raise HandshakeError(f"verify_client rejected: {verify_reply!r}")
    return verify_reply
