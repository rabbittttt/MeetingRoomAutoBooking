"""Local W3 fallback credentials protected by Windows DPAPI CurrentUser.

All deployment profiles share this user-local file. Neither the on-disk
envelope nor temporary files contain a plaintext account, password or origin.
Public errors deliberately omit underlying exception messages and payloads.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


class CredentialError(RuntimeError):
    """A sanitized credential-storage or validation failure."""


@dataclass(frozen=True)
class LoginCredentials:
    username: str
    password: str = field(repr=False)
    login_origin: str


_MAGIC = b"MRAB-W3-DPAPI\x00"
_MAX_FILE_BYTES = 1024 * 1024
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_ORIGIN_ERROR = "登录地址必须是有效的 HTTPS 源地址，不得包含账号、路径、查询或片段。"


def credential_path() -> Path:
    """Use the common application base, never the project or profile folder."""
    try:
        base = os.environ.get("LOCALAPPDATA", "")
        if not base or not Path(base).is_absolute():
            raise ValueError
        return Path(base) / "MeetingRoomAutoBooking" / "w3_credentials.dat"
    except Exception:
        raise CredentialError("无法确定本机凭据保存目录。") from None


def normalize_login_origin(value: str) -> str:
    """Normalize one HTTPS origin, rejecting URLs with additional authority/data."""
    try:
        if (not isinstance(value, str) or not value or value != value.strip()
                or any(ord(char) < 32 or char.isspace() for char in value)
                or any(char in value for char in "\\?#")):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme.lower() != "https" or not parts.netloc
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or "%" in parts.netloc):
            raise ValueError
        host = parts.hostname
        if not host:
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        # An empty explicit port is not a complete origin.
        if parts.netloc.endswith(":"):
            raise ValueError
        if ":" in host:
            if re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parts.netloc) is None:
                raise ValueError
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            if len(host) > 253 or any(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                    for label in host.split(".")):
                raise ValueError
        return "https://" + host + (f":{port}" if port not in (None, 443) else "")
    except Exception:
        raise CredentialError(_ORIGIN_ERROR) from None


def _validated(username: str, password: str, login_origin: str) -> LoginCredentials:
    if not isinstance(username, str) or not username.strip():
        raise CredentialError("请输入备用登录账号。")
    if not isinstance(password, str) or password == "":
        raise CredentialError("请输入备用登录密码。")
    # Passwords are never stripped or otherwise transformed.
    return LoginCredentials(username.strip(), password, normalize_login_origin(login_origin))


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    """Use only CurrentUser protection and prohibit any DPAPI user interface."""
    try:
        if os.name != "nt" or not data or len(data) > _MAX_FILE_BYTES:
            raise ValueError
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        operation.argtypes = [ctypes.POINTER(_DataBlob),
                              ctypes.POINTER(ctypes.c_wchar_p) if decrypt else ctypes.c_wchar_p,
                              ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_uint32, ctypes.POINTER(_DataBlob)]
        operation.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = _DataBlob()
        try:
            # No CRYPTPROTECT_LOCAL_MACHINE flag: another Windows user cannot
            # simply open the copied file. No description is stored either.
            if not operation(ctypes.byref(source), None, None, None, None,
                             _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)):
                raise ValueError
            if not output.pbData or not 0 < output.cbData <= _MAX_FILE_BYTES:
                raise ValueError
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.memset(buffer, 0, len(data))
            if output.pbData:
                if decrypt and output.cbData <= _MAX_FILE_BYTES:
                    ctypes.memset(output.pbData, 0, output.cbData)
                kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
    except Exception:
        raise CredentialError("Windows 无法保护或读取本机凭据。") from None


def save_credentials(username: str, password: str, login_origin: str) -> None:
    """Encrypt first, then atomically replace the previous encrypted file."""
    temporary: Path | None = None
    try:
        credentials = _validated(username, password, login_origin)
        payload = json.dumps({"schema": 1, "username": credentials.username,
                              "password": credentials.password,
                              "login_origin": credentials.login_origin},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = _MAGIC + _dpapi(payload, decrypt=False)
        if len(encrypted) > _MAX_FILE_BYTES:
            raise ValueError
        destination = credential_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix="w3_credentials.dat.", suffix=".tmp",
                                         dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        temporary = None
    except Exception:
        raise CredentialError("无法保存备用登录信息；原有凭据未更新。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass  # Any surviving temporary file contains DPAPI ciphertext only.


def load_credentials() -> LoginCredentials | None:
    """Return None only when no file exists; corrupt/unreadable data is an error."""
    try:
        path = credential_path()
        try:
            with path.open("rb") as stream:
                encrypted = stream.read(_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return None
        if (len(encrypted) > _MAX_FILE_BYTES or not encrypted.startswith(_MAGIC)
                or len(encrypted) <= len(_MAGIC)):
            raise ValueError
        decoded = _dpapi(encrypted[len(_MAGIC):], decrypt=True).decode("utf-8")
        payload = json.loads(decoded)
        if (not isinstance(payload, dict) or set(payload) != {
                "schema", "username", "password", "login_origin"}
                or type(payload["schema"]) is not int or payload["schema"] != 1):
            raise ValueError
        return _validated(payload["username"], payload["password"], payload["login_origin"])
    except Exception:
        raise CredentialError("无法读取备用登录信息；文件可能损坏或不属于当前 Windows 账号。") from None


def delete_credentials() -> None:
    try:
        credential_path().unlink(missing_ok=True)
    except Exception:
        raise CredentialError("无法删除本机备用登录信息。") from None
