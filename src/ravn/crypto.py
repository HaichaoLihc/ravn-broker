"""Credential-specific AEAD storage, not a generic vault."""

import base64
import hashlib
import hmac
import os
import stat

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ravn.common import canonical
from ravn.config import Config


def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 16384
        ):
            raise ValueError("Secret file must be an owner-only regular file")
        return os.read(fd, 16385).strip()
    finally:
        os.close(fd)


class Cipher:
    def __init__(self, config: Config):
        self.key_id = config.secrets.managed.active_key_id
        key = base64.b64decode(private_file(config.secrets.managed.key_source.path), validate=True)
        if len(key) != 32:
            raise ValueError("Master key must decode to 32 random bytes")
        self.aead = AESGCM(key)
        self.deployment = config.deployment_id
        self.fingerprint_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.deployment.encode(),
            info=b"ravn/fingerprint/v1",
        ).derive(key)

    def aad(self, app: str, tenant: str, connection: str) -> bytes:
        return canonical([self.deployment, app, tenant, connection, 1, "credential-v1"])

    def encrypt(self, value: str, app: str, tenant: str, connection: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self.aead.encrypt(nonce, value.encode(), self.aad(app, tenant, connection))

    def decrypt(self, value: bytes, app: str, tenant: str, connection: str) -> str:
        return self.aead.decrypt(value[:12], value[12:], self.aad(app, tenant, connection)).decode()

    def fingerprint(self, context: object) -> str:
        return hmac.new(
            self.fingerprint_key, b"arguments-v1\0" + canonical(context), hashlib.sha256
        ).hexdigest()
