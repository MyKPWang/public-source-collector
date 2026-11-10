from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..models import Credentials


class VaultError(RuntimeError):
    pass


class EncryptedFileCredentialVault:
    """Credential and fakeid encryption with Keychain/keyring and file fallback."""

    def __init__(self, credentials_path: Path, *, service_name: str = "wechat-list-collector"):
        self.credentials_path = credentials_path
        self.service_name = service_name
        self._fernet = Fernet(self._load_or_create_key())

    def load_wechat(self) -> Credentials:
        try:
            payload = json.loads(self._fernet.decrypt(self.credentials_path.read_bytes()).decode("utf-8"))
            return Credentials(token=str(payload["token"]), cookie=str(payload["cookie"]))
        except FileNotFoundError as error:
            raise VaultError("no WeChat credentials are available") from error
        except (InvalidToken, ValueError, KeyError, TypeError) as error:
            raise VaultError("stored WeChat credentials cannot be decrypted") from error

    def replace_wechat(self, credentials: Credentials) -> None:
        payload = json.dumps({"token": credentials.token, "cookie": credentials.cookie}, separators=(",", ":")).encode("utf-8")
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.credentials_path.with_suffix(".tmp")
        temporary.write_bytes(self._fernet.encrypt(payload))
        os.chmod(temporary, 0o600)
        temporary.replace(self.credentials_path)

    def load_weread(self) -> dict[str, str]:
        """读取微信读书凭据（公众号推文抓取用）。"""
        path = self.credentials_path.with_name("weread_credentials.enc")
        try:
            payload = json.loads(self._fernet.decrypt(path.read_bytes()).decode("utf-8"))
        except FileNotFoundError as error:
            raise VaultError("no WeRead credentials are available") from error
        except (InvalidToken, ValueError) as error:
            raise VaultError("stored WeRead credentials cannot be decrypted") from error
        return {str(key): str(value) for key, value in payload.items()}

    def load_weread_accounts(self) -> list[dict[str, str]]:
        """读取微信读书账号池（支持单账号与多账号两种落盘格式）。"""
        path = self.credentials_path.with_name("weread_credentials.enc")
        try:
            payload = json.loads(self._fernet.decrypt(path.read_bytes()).decode("utf-8"))
        except FileNotFoundError as error:
            raise VaultError("no WeRead credentials are available") from error
        except (InvalidToken, ValueError) as error:
            raise VaultError("stored WeRead credentials cannot be decrypted") from error
        if isinstance(payload, list):
            return [
                {str(key): str(value) for key, value in item.items()}
                for item in payload
                if isinstance(item, dict)
            ]
        return [{str(key): str(value) for key, value in payload.items()}]

    def replace_weread(self, credentials: dict[str, str]) -> None:
        """写入微信读书凭据（vid / skey / accessToken / refreshToken ...）。"""
        path = self.credentials_path.with_name("weread_credentials.enc")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(self._fernet.encrypt(json.dumps(credentials, ensure_ascii=False).encode("utf-8")))
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def replace_weread_accounts(self, accounts: list[dict[str, str]]) -> None:
        """写入微信读书账号池（原子替换，mode 600）。"""
        path = self.credentials_path.with_name("weread_credentials.enc")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(self._fernet.encrypt(json.dumps(accounts, ensure_ascii=False).encode("utf-8")))
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def encrypt_text(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt_text(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as error:
            raise VaultError("encrypted value cannot be decrypted") from error

    def _load_or_create_key(self) -> bytes:
        environment_key = os.getenv("WECHAT_COLLECTOR_VAULT_KEY")
        if environment_key:
            return environment_key.encode("ascii")
        try:
            import keyring

            key = keyring.get_password(self.service_name, "fernet-key")
            if key is None:
                key = Fernet.generate_key().decode("ascii")
                keyring.set_password(self.service_name, "fernet-key", key)
            return key.encode("ascii")
        except Exception:
            fallback = self.credentials_path.with_name(".vault.key")
            if fallback.exists():
                return fallback.read_bytes().strip()
            fallback.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            fallback.write_bytes(key)
            os.chmod(fallback, 0o600)
            return key


class InMemoryCredentialVault:
    """Test double that intentionally does not write credentials or key material."""

    def __init__(self, credentials: Credentials | None = None):
        self.credentials = credentials

    def load_wechat(self) -> Credentials:
        if self.credentials is None:
            raise VaultError("no WeChat credentials are available")
        return self.credentials

    def replace_wechat(self, credentials: Credentials) -> None:
        self.credentials = credentials

    @staticmethod
    def encrypt_text(value: str) -> bytes:
        return value.encode("utf-8")

    @staticmethod
    def decrypt_text(ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")
