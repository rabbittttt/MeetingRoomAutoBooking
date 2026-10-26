"""Offline authorization regressions; all signing keys are disposable test keys."""
import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import account_auth as auth
import license_admin as admin

_UNSPECIFIED = object()


class AccountAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public_key = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode("ascii")
        self.payload = {
            "schema_version": 1, "app_id": "MeetingRoomAutoBooking",
            "license_version": 1, "accounts": ["z00523094"],
        }
        self.document = self.signed(self.payload)
        self.directory = tempfile.TemporaryDirectory(prefix="booking_auth_test_")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / auth.LICENSE_FILENAME
        patcher = patch.dict(sys.modules, {
            "account_public_key": types.SimpleNamespace(PUBLIC_KEY_BASE64=self.public_key),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def signed(self, payload):
        payload = copy.deepcopy(payload)
        return {"payload": payload, "signature": base64.b64encode(
            self.key.sign(auth.canonical_payload_bytes(payload)),
        ).decode("ascii")}

    def verify(self, document=_UNSPECIFIED, username="z00523094"):
        return auth.verify_document(
            self.document if document is _UNSPECIFIED else document, username, self.public_key,
        )

    def write_document(self, document=None):
        self.path.write_text(json.dumps(
            self.document if document is None else document,
        ), encoding="utf-8")

    def require(self):
        with patch.object(auth, "get_current_windows_account", return_value="z00523094"):
            return auth.require_authorized_account(self.path)

    def test_allows_signed_current_account(self):
        self.assertEqual("z00523094", self.verify())

    def test_normalizes_windows_domain_and_case(self):
        self.assertEqual("z00523094", self.verify(username="COMPANY\\Z00523094"))

    def test_denies_unlisted_account_with_actionable_error(self):
        with self.assertRaisesRegex(auth.AuthorizationError, "z00000001.*未获得使用授权"):
            self.verify(username="z00000001")

    def test_tampered_account_list_fails_signature(self):
        document = copy.deepcopy(self.document)
        document["payload"]["accounts"].append("z00000001")
        with self.assertRaisesRegex(auth.AuthorizationError, "签名无效"):
            self.verify(document, username="z00000001")

    def test_wrong_signing_key_fails(self):
        other = Ed25519PrivateKey.generate()
        document = copy.deepcopy(self.document)
        document["signature"] = base64.b64encode(other.sign(
            auth.canonical_payload_bytes(document["payload"]),
        )).decode("ascii")
        with self.assertRaises(auth.AuthorizationError):
            self.verify(document)

    def test_invalid_signature_encoding_and_length_fail(self):
        for signature in ("???", "", "é", base64.b64encode(b"wrong").decode("ascii"), None):
            with self.subTest(signature_type=type(signature).__name__):
                document = copy.deepcopy(self.document)
                document["signature"] = signature
                with self.assertRaises(auth.AuthorizationError):
                    self.verify(document)

    def test_invalid_public_key_fails(self):
        for key in ("???", base64.b64encode(b"wrong").decode("ascii")):
            with self.subTest(key=key):
                with self.assertRaises(auth.AuthorizationError):
                    auth.verify_document(self.document, "z00523094", key)

    def test_document_shape_is_strict(self):
        for document in ([], None, {}, {**self.document, "accounts": ["z00000001"]}):
            with self.subTest(document_type=type(document).__name__):
                with self.assertRaises(auth.AuthorizationError):
                    self.verify(document)

    def test_payload_shape_is_strict(self):
        for payload in (None, [], {**self.payload, "allow_all": True}):
            with self.subTest(payload_type=type(payload).__name__):
                document = copy.deepcopy(self.document)
                document["payload"] = payload
                with self.assertRaises(auth.AuthorizationError):
                    self.verify(document)

    def test_signed_wrong_app_is_rejected(self):
        payload = {**self.payload, "app_id": "AnotherApplication"}
        with self.assertRaises(auth.AuthorizationError):
            self.verify(self.signed(payload))

    def test_signed_unknown_schema_or_license_version_rejected(self):
        for field in ("schema_version", "license_version"):
            for value in (True, "1", 2, 0):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(auth.AuthorizationError):
                        self.verify(self.signed({**self.payload, field: value}))

    def test_signed_empty_or_malformed_accounts_rejected(self):
        values = [[], None, "z00523094", [""], ["Z00523094"],
                  ["COMPANY\\z00523094"], ["z00523094", "z00523094"],
                  ["z00523094", 12], ["user@example.com"], ["*"]]
        for accounts in values:
            with self.subTest(accounts=accounts):
                with self.assertRaises(auth.AuthorizationError):
                    self.verify(self.signed({**self.payload, "accounts": accounts}))

    def test_account_count_limit_rejected(self):
        accounts = [f"z{i:08d}" for i in range(auth.MAX_ACCOUNTS + 1)]
        with self.assertRaises(auth.AuthorizationError):
            self.verify(self.signed({**self.payload, "accounts": accounts}))

    def test_verification_does_not_mutate_document(self):
        before = copy.deepcopy(self.document)
        self.verify()
        self.assertEqual(before, self.document)

    def test_canonical_payload_does_not_depend_on_key_order(self):
        reordered = dict(reversed(list(self.payload.items())))
        self.document["payload"] = reordered
        self.assertEqual("z00523094", self.verify())

    def test_external_file_allows_valid_document(self):
        self.write_document()
        self.assertEqual("z00523094", self.require())

    def test_missing_external_file_denies(self):
        with self.assertRaisesRegex(auth.AuthorizationError, "未找到账号授权"):
            self.require()

    def test_empty_external_file_denies(self):
        self.path.write_bytes(b"")
        with self.assertRaises(auth.AuthorizationError):
            self.require()

    def test_oversized_external_file_denies_before_parse(self):
        self.path.write_bytes(b" " * (auth.MAX_LICENSE_BYTES + 1))
        with self.assertRaisesRegex(auth.AuthorizationError, "大小限制"):
            self.require()

    def test_malformed_json_and_bad_utf8_deny(self):
        for raw in (b"{bad json", b"\xff\xfe", b"[1, 2, 3]"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(auth.AuthorizationError):
                    self.require()

    def test_duplicate_json_keys_deny(self):
        raw = json.dumps(self.document)
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        self.path.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(auth.AuthorizationError, "内容损坏"):
            self.require()

    def test_unreadable_license_denies(self):
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(auth.AuthorizationError, "无法读取"):
                self.require()

    def test_missing_public_key_denies(self):
        self.write_document()
        with patch.dict(sys.modules, {"account_public_key": None}):
            with self.assertRaisesRegex(auth.AuthorizationError, "公钥缺失"):
                self.require()

    def test_non_windows_denies_even_if_username_environment_is_allowed(self):
        with patch.object(auth.sys, "platform", "linux"), patch.dict(os.environ, {"USERNAME": "z00523094"}):
            with self.assertRaisesRegex(auth.AuthorizationError, "仅支持 Windows"):
                auth.get_current_windows_account()

    def test_os_api_identity_wins_over_environment_spoof(self):
        with patch.object(auth.sys, "platform", "win32"), \
                patch.object(auth, "_read_windows_username", return_value="COMPANY\\z00000001"), \
                patch.dict(os.environ, {"USERNAME": "z00523094", "USER": "z00523094"}):
            self.assertEqual("z00000001", auth.get_current_windows_account())
            self.write_document()
            with self.assertRaisesRegex(auth.AuthorizationError, "z00000001.*未获得使用授权"):
                auth.require_authorized_account(self.path)

    @unittest.skipUnless(sys.platform == "win32", "Requires Windows GetUserNameW")
    def test_real_windows_api_ignores_username_environment(self):
        actual = auth._read_windows_username()
        try:
            normalized = auth.normalize_account(actual)
        except auth.AuthorizationError:
            normalized = None  # 开发电脑账号可能含空格，仍须拒绝被环境变量冒充。
        spoof = "not-the-current-os-account"
        with patch.dict(os.environ, {"USERNAME": spoof, "USER": spoof}):
            self.assertEqual(actual, auth._read_windows_username())
            if normalized is None:
                with self.assertRaises(auth.AuthorizationError):
                    auth.get_current_windows_account()
            else:
                self.assertEqual(normalized, auth.get_current_windows_account())

    def test_os_api_failure_denies(self):
        with patch.object(auth.sys, "platform", "win32"), \
                patch.object(auth.ctypes, "WinDLL", side_effect=OSError("API unavailable")):
            with self.assertRaisesRegex(auth.AuthorizationError, "无法读取当前 Windows"):
                auth.get_current_windows_account()

    def test_blank_os_identity_denies(self):
        with patch.object(auth.sys, "platform", "win32"), \
                patch.object(auth, "_read_windows_username", return_value=""):
            with self.assertRaises(auth.AuthorizationError):
                auth.get_current_windows_account()

    def test_frozen_license_is_next_to_exe_not_bundle_temp(self):
        executable = str(Path(self.directory.name) / "app.exe")
        with patch.object(auth.sys, "frozen", True, create=True), \
                patch.object(auth.sys, "executable", executable):
            self.assertEqual(Path(executable).resolve().parent, auth.application_directory())

    def test_source_license_is_next_to_module(self):
        with patch.object(auth.sys, "frozen", False, create=True):
            self.assertEqual(Path(auth.__file__).resolve().parent, auth.application_directory())

    def test_admin_signs_external_list_accepted_by_verifier(self):
        key_path = Path(self.directory.name) / "temporary_test_key.pem"
        key_path.write_bytes(self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        admin.sign_license(key_path, ["Z00523094", "z00000001"], self.path)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual("z00000001", self.verify(document, username="z00000001"))
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_admin_normalizes_and_deduplicates_editor_input(self):
        self.assertEqual(["z00523094", "z00000001"], admin.parse_accounts(
            "Z00523094，z00000001\nz00523094",
        ))

    def test_admin_empty_list_does_not_replace_existing_license(self):
        self.write_document()
        before = self.path.read_bytes()
        with self.assertRaises(auth.AuthorizationError):
            admin.sign_license(Path(self.directory.name) / "absent.pem", [], self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_admin_bad_private_key_does_not_replace_existing_license(self):
        self.write_document()
        before = self.path.read_bytes()
        key_path = Path(self.directory.name) / "bad_key.pem"
        key_path.write_bytes(b"not a private key")
        with self.assertRaisesRegex(auth.AuthorizationError, "无法读取管理员私钥"):
            admin.sign_license(key_path, ["z00523094"], self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_admin_missing_private_key_does_not_replace_existing_license(self):
        self.write_document()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(auth.AuthorizationError, "无法读取管理员私钥"):
            admin.sign_license(Path(self.directory.name) / "absent.pem", ["z00523094"], self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_admin_self_test_does_not_read_real_private_key(self):
        marker = Path(self.directory.name) / "self_test_marker.txt"
        with patch.object(Path, "read_bytes", side_effect=AssertionError("No private key read")):
            self.assertEqual(0, admin.main(["--self-test", str(marker)]))
        self.assertEqual("license-admin-ok", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
