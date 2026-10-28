"""Only fictitious credentials in isolated temporary folders; no network."""
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch

import w3_credentials as credentials


ORIGIN = "https://login.example.test"
USERNAME = "fictional-测试账号"
PASSWORD = "  fictional-密码-🔐-only  "


class CredentialValidationTests(unittest.TestCase):
    def test_https_origin_normalization(self):
        for supplied, expected in (
                ("https://login.huawei.com", "https://login.huawei.com"),
                ("HTTPS://LOGIN.EXAMPLE.TEST:443/", ORIGIN),
                ("https://login.example.test:8443", ORIGIN + ":8443"),
                ("https://[2001:db8::1]:443/", "https://[2001:db8::1]")):
            with self.subTest(value=supplied):
                self.assertEqual(credentials.normalize_login_origin(supplied), expected)

    def test_rejects_non_origins_without_echoing_input(self):
        invalid = ["", None, 123, "http://login.example.test", "//login.example.test",
                   "https://fictional-user:fictional-secret@login.example.test",
                   "https://@login.example.test", ORIGIN + "/login", ORIGIN + "?",
                   ORIGIN + "#", ORIGIN + "?password=fictional-secret", ORIGIN + "#secret",
                   ORIGIN + ":", ORIGIN + ":65536", ORIGIN + ":0", "https://",
                   "https://login.example.test\\@evil.test", "https://login.exa%mple.test",
                   "https://[::1]extra", "https://[::1]extra:443",
                   " https://login.example.test", "https://login.example.test\n",
                   "https://login.example.test\t", "https://login.example.test./"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(credentials.CredentialError) as caught:
                credentials.normalize_login_origin(value)
            self.assertNotIn("fictional-secret", str(caught.exception))
            self.assertNotIn("fictional-user", str(caught.exception))

    def test_frozen_data_class_omits_password_from_repr(self):
        value = credentials.LoginCredentials(USERNAME, PASSWORD, ORIGIN)
        self.assertNotIn(PASSWORD, repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.password = "changed"

    def test_missing_local_appdata_never_falls_back_to_project_directory(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            with self.assertRaises(credentials.CredentialError):
                credentials.credential_path()
        with patch.dict(os.environ, {"LOCALAPPDATA": "relative-folder"}):
            with self.assertRaises(credentials.CredentialError):
                credentials.credential_path()


class CredentialStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="booking-w3-fictitious-")
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": self.directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = Path(self.directory.name) / "MeetingRoomAutoBooking" / "w3_credentials.dat"

    def assert_redacted(self, operation):
        try:
            operation()
        except credentials.CredentialError as exc:
            rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            for secret in (USERNAME, PASSWORD, "fictional-upstream-secret"):
                self.assertNotIn(secret, str(exc))
                self.assertNotIn(secret, rendered)
            self.assertTrue(exc.__suppress_context__)
        else:
            self.fail("CredentialError was not raised")

    def test_common_local_path_no_file_and_idempotent_delete(self):
        self.assertEqual(credentials.credential_path(), self.path)
        self.assertIsNone(credentials.load_credentials())
        credentials.delete_credentials()
        credentials.delete_credentials()
        self.assertFalse(self.path.exists())

    def test_empty_credentials_fail_before_encryption_or_file_creation(self):
        with patch.object(credentials, "_dpapi") as encrypt:
            for username, password in (("", PASSWORD), ("  ", PASSWORD),
                                       (USERNAME, ""), (USERNAME, None), (None, PASSWORD)):
                with self.subTest(username_empty=not username, password_empty=not password):
                    self.assert_redacted(lambda: credentials.save_credentials(username, password, ORIGIN))
            encrypt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_invalid_envelope_and_oversize_data_are_redacted(self):
        self.path.parent.mkdir(parents=True)
        for data in (b"", b"plaintext-fictional-upstream-secret", credentials._MAGIC,
                     credentials._MAGIC + b"x" * credentials._MAX_FILE_BYTES):
            with self.subTest(length=len(data)):
                self.path.write_bytes(data)
                with patch.object(credentials, "_dpapi") as decrypt:
                    self.assert_redacted(credentials.load_credentials)
                    decrypt.assert_not_called()

    def test_decryption_and_payload_errors_never_echo_secrets(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(credentials._MAGIC + b"fake-encrypted-data")
        with patch.object(credentials, "_dpapi", side_effect=RuntimeError("fictional-upstream-secret")):
            self.assert_redacted(credentials.load_credentials)
        valid = {"schema": 1, "username": USERNAME, "password": PASSWORD, "login_origin": ORIGIN}
        invalid = [b"fictional-upstream-secret", b"\xff", b"[]",
                   json.dumps({**valid, "schema": True}).encode(),
                   json.dumps({**valid, "schema": 2}).encode(),
                   json.dumps({**valid, "unexpected": "fictional-upstream-secret"}).encode(),
                   json.dumps({**valid, "password": ""}).encode(),
                   json.dumps({**valid, "login_origin": "http://fictional-upstream-secret"}).encode()]
        for payload in invalid:
            with self.subTest(payload_length=len(payload)), patch.object(credentials, "_dpapi", return_value=payload):
                self.assert_redacted(credentials.load_credentials)

    def test_filesystem_failures_are_redacted(self):
        with patch.object(Path, "open", side_effect=PermissionError("fictional-upstream-secret")):
            self.assert_redacted(credentials.load_credentials)
        with patch.object(Path, "unlink", side_effect=PermissionError("fictional-upstream-secret")):
            self.assert_redacted(credentials.delete_credentials)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI CurrentUser is required")
    def test_real_dpapi_roundtrip_preserves_unicode_and_password_spaces(self):
        credentials.save_credentials(USERNAME, PASSWORD, ORIGIN + "/")
        restored = credentials.load_credentials()
        self.assertEqual(restored, credentials.LoginCredentials(USERNAME, PASSWORD, ORIGIN))
        encrypted = self.path.read_bytes()
        self.assertTrue(encrypted.startswith(credentials._MAGIC))
        for plaintext in (USERNAME, PASSWORD, ORIGIN):
            self.assertNotIn(plaintext.encode("utf-8"), encrypted)
            self.assertNotIn(plaintext.encode("utf-16-le"), encrypted)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        credentials.save_credentials("another-fictional-user", " new-fictional-password ", ORIGIN)
        self.assertEqual(credentials.load_credentials(), credentials.LoginCredentials(
            "another-fictional-user", " new-fictional-password ", ORIGIN))
        credentials.delete_credentials()
        self.assertIsNone(credentials.load_credentials())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI CurrentUser is required")
    def test_corrupt_dpapi_ciphertext_is_an_error_not_missing_credentials(self):
        credentials.save_credentials(USERNAME, PASSWORD, ORIGIN)
        encrypted = bytearray(self.path.read_bytes())
        encrypted[-1] ^= 0x80
        self.path.write_bytes(encrypted)
        self.assert_redacted(credentials.load_credentials)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI CurrentUser is required")
    def test_failed_atomic_replace_preserves_old_credentials_and_removes_temp(self):
        credentials.save_credentials(USERNAME, PASSWORD, ORIGIN)
        old_ciphertext = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=PermissionError("fictional-upstream-secret")):
            self.assert_redacted(lambda: credentials.save_credentials("other-fictional", "new-fictional", ORIGIN))
        self.assertEqual(self.path.read_bytes(), old_ciphertext)
        self.assertEqual(credentials.load_credentials().password, PASSWORD)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI CurrentUser is required")
    def test_failed_encryption_or_flush_never_replaces_old_file(self):
        credentials.save_credentials(USERNAME, PASSWORD, ORIGIN)
        old_ciphertext = self.path.read_bytes()
        for name in ("_dpapi", "fsync"):
            owner = credentials if name == "_dpapi" else credentials.os
            with self.subTest(stage=name), patch.object(owner, name, side_effect=OSError("fictional-upstream-secret")):
                self.assert_redacted(lambda: credentials.save_credentials("other-fictional", "new-fictional", ORIGIN))
            self.assertEqual(self.path.read_bytes(), old_ciphertext)
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
