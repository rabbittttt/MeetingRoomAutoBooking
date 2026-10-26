"""Offline, fail-closed account authorization for the booking application.

Only the public verification key is distributed with the application.  Windows
provides the current account; environment variables are deliberately not used.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import re
import sys
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


APP_ID = "MeetingRoomAutoBooking"
SCHEMA_VERSION = 1
LICENSE_VERSION = 1
LICENSE_FILENAME = "账号授权.json"
MAX_LICENSE_BYTES = 64 * 1024
MAX_ACCOUNTS = 1000
_ACCOUNT_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z", re.ASCII)


class AuthorizationError(RuntimeError):
    """The application must not start booking for the current Windows account."""


def normalize_account(account: str) -> str:
    """Return a bare, lower-case account, accepting Windows DOMAIN\\user input."""
    if not isinstance(account, str):
        raise AuthorizationError("账号格式不正确，请使用公司账号，例如 z00523094。")
    account = account.strip()
    if "\\" in account:
        parts = account.split("\\")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise AuthorizationError("Windows 账号格式不正确，无法确认当前使用者。")
        account = parts[1]
    account = account.lower()
    if not _ACCOUNT_PATTERN.fullmatch(account):
        raise AuthorizationError("账号格式不正确，请使用公司账号，例如 z00523094。")
    return account


def _read_windows_username() -> str:
    """Read the logged-on identity through GetUserNameW, never USERNAME."""
    try:
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        get_user_name = api.GetUserNameW
        get_user_name.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        get_user_name.restype = wintypes.BOOL
        size = wintypes.DWORD(257)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not get_user_name(buffer, ctypes.byref(size)):
            error = ctypes.get_last_error()
            if error != 122 or not 1 < size.value <= 32768:
                raise OSError(error, "GetUserNameW failed")
            buffer = ctypes.create_unicode_buffer(size.value)
            if not get_user_name(buffer, ctypes.byref(size)):
                raise OSError(ctypes.get_last_error(), "GetUserNameW failed")
        return buffer.value
    except Exception as exc:
        raise AuthorizationError(
            "无法读取当前 Windows 登录账号。请使用公司 Windows 账号登录后重试。"
        ) from exc


def get_current_windows_account() -> str:
    if sys.platform != "win32":
        raise AuthorizationError("账号授权仅支持 Windows，请在公司 Windows 账号下运行。")
    return normalize_account(_read_windows_username())


def application_directory() -> Path:
    """Locate the external license next to the EXE, including one-file builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def validate_accounts(accounts: Any) -> list[str]:
    if type(accounts) is not list or not 1 <= len(accounts) <= MAX_ACCOUNTS:
        raise AuthorizationError("授权名单为空或数量不正确，请联系管理员重新签发。")
    for account in accounts:
        if not isinstance(account, str) or normalize_account(account) != account:
            raise AuthorizationError("授权名单含有无效账号，请联系管理员重新签发。")
    if len(set(accounts)) != len(accounts):
        raise AuthorizationError("授权名单含有重复账号，请联系管理员重新签发。")
    return accounts


def verify_document(
    document: Any, username: str, public_key_base64: str | None = None,
) -> str:
    """Verify a parsed license and account, with no filesystem or OS side effects."""
    if type(document) is not dict or set(document) != {"payload", "signature"}:
        raise AuthorizationError("授权文件格式不正确，请联系管理员重新签发。")
    payload = document["payload"]
    if type(payload) is not dict or set(payload) != {
        "schema_version", "app_id", "license_version", "accounts",
    }:
        raise AuthorizationError("授权内容格式不正确，请联系管理员重新签发。")
    if (type(payload["schema_version"]) is not int
            or payload["schema_version"] != SCHEMA_VERSION
            or type(payload["license_version"]) is not int
            or payload["license_version"] != LICENSE_VERSION
            or payload["app_id"] != APP_ID):
        raise AuthorizationError("授权文件不适用于当前程序，请联系管理员重新签发。")
    accounts = validate_accounts(payload["accounts"])
    signature = document["signature"]
    if not isinstance(signature, str):
        raise AuthorizationError("授权签名格式不正确，请联系管理员重新签发。")
    if public_key_base64 is None:
        try:
            from account_public_key import PUBLIC_KEY_BASE64
            public_key_base64 = PUBLIC_KEY_BASE64
        except Exception as exc:
            raise AuthorizationError("程序授权公钥缺失，请重新获取完整程序包。") from exc
    try:
        key_bytes = base64.b64decode(public_key_base64, validate=True)
        signature_bytes = base64.b64decode(signature, validate=True)
        if len(key_bytes) != 32 or len(signature_bytes) != 64:
            raise ValueError("Invalid Ed25519 length")
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(
            signature_bytes, canonical_payload_bytes(payload),
        )
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise AuthorizationError(
            "授权文件签名无效或文件已被修改，请联系管理员获取新的账号授权.json。"
        ) from exc
    normalized = normalize_account(username)
    if normalized not in accounts:
        raise AuthorizationError(
            f"当前 Windows 账号 {normalized} 未获得使用授权。"
            "请联系管理员将该账号加入名单，并替换 EXE 同目录的账号授权.json。"
        )
    return normalized


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def require_authorized_account(path: str | Path | None = None) -> str:
    """Read the external signed list and return the verified Windows account."""
    username = get_current_windows_account()
    license_path = Path(path) if path is not None else application_directory() / LICENSE_FILENAME
    try:
        with license_path.open("rb") as handle:
            raw = handle.read(MAX_LICENSE_BYTES + 1)
    except FileNotFoundError as exc:
        raise AuthorizationError(
            "未找到账号授权.json。请将管理员提供的授权文件放在 EXE 同一文件夹后重试。"
        ) from exc
    except OSError as exc:
        raise AuthorizationError("无法读取账号授权.json，请检查文件权限或联系管理员。") from exc
    if not raw or len(raw) > MAX_LICENSE_BYTES:
        raise AuthorizationError("授权文件为空或超过大小限制，请联系管理员重新签发。")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AuthorizationError("授权文件内容损坏，请联系管理员重新签发。") from exc
    return verify_document(document, username)
