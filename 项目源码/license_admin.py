"""Owner-only editor and signer for external account authorization files."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from account_auth import (
    APP_ID, LICENSE_FILENAME, LICENSE_VERSION, MAX_LICENSE_BYTES, SCHEMA_VERSION,
    AuthorizationError, canonical_payload_bytes, normalize_account, validate_accounts,
    verify_document,
)


def admin_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def parse_accounts(value: str | list[str]) -> list[str]:
    text = value if isinstance(value, str) else "\n".join(value)
    items = [normalize_account(item) for item in re.split(r"[,，;；\s]+", text.strip()) if item]
    # Preserve input order while avoiding accidental double-entry in the editor.
    accounts = list(dict.fromkeys(items))
    validate_accounts(accounts)
    return accounts


def sign_license(key_path: Path, accounts: list[str], output: Path) -> None:
    """Sign using the supplied owner key. Never print or embed private key data."""
    accounts = parse_accounts(accounts)
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise AuthorizationError(
            "无法读取管理员私钥。请将 admin_private_key.pem 放在管理工具同目录，"
            "或用 --key 指定正确的私钥文件。"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AuthorizationError("私钥类型不正确，必须使用本程序配套的 Ed25519 管理员私钥。")
    payload = {
        "schema_version": SCHEMA_VERSION, "app_id": APP_ID,
        "license_version": LICENSE_VERSION, "accounts": accounts,
    }
    document = {
        "payload": payload,
        "signature": base64.b64encode(key.sign(canonical_payload_bytes(payload))).decode("ascii"),
    }
    encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_LICENSE_BYTES:
        raise AuthorizationError("名单生成的授权文件超过大小限制，请减少账号数量。")
    output.parent.mkdir(parents=True, exist_ok=True)
    # A complete temporary document replaces the old license only after writing.
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_bytes(encoded)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_editor(key_path: Path, output: Path, accounts_path: Path) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("会议室预约 · 账号授权管理")
    root.geometry("610x480")
    root.minsize(550, 420)
    container = ttk.Frame(root, padding=20)
    container.pack(fill="both", expand=True)
    ttk.Label(container, text="允许使用的公司 / Windows 账号", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
    ttk.Label(container, text="每行一个账号，保存后将生成新的账号授权.json。", wraplength=550).pack(anchor="w", pady=(8, 12))
    editor = tk.Text(container, height=12, font=("Consolas", 12), undo=True)
    editor.pack(fill="both", expand=True)
    try:
        initial = accounts_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        initial = ""
    except OSError as exc:
        messagebox.showerror("无法读取名单", f"无法读取 accounts.txt：{exc}", parent=root)
        initial = ""
    editor.insert("1.0", initial)
    ttk.Label(container, text=f"授权输出：{output}", wraplength=550).pack(anchor="w", pady=(12, 6))
    status = tk.StringVar(value="仅管理员保管私钥；给使用者发送账号授权.json 即可。")
    ttk.Label(container, textvariable=status, wraplength=550).pack(anchor="w", pady=(0, 12))

    def save_and_sign() -> None:
        try:
            accounts = parse_accounts(editor.get("1.0", "end"))
            sign_license(key_path, accounts, output)
            accounts_path.write_text("\n".join(accounts) + "\n", encoding="utf-8")
        except (AuthorizationError, OSError) as exc:
            messagebox.showerror("签发失败", str(exc), parent=root)
            return
        status.set(f"已签发 {len(accounts)} 个账号。请替换使用者 EXE 同目录的账号授权.json。")
        messagebox.showinfo("签发完成", f"已生成授权文件：\n{output}", parent=root)

    ttk.Button(container, text="保存名单并签发授权文件", command=save_and_sign).pack(anchor="e")
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="会议室预约账号授权管理（仅管理员使用）")
    directory = admin_directory()
    parser.add_argument("--key", type=Path, default=directory / "admin_private_key.pem", help="管理员 Ed25519 PEM 私钥路径")
    parser.add_argument("--accounts", nargs="+", help="要授权的账号；省略时打开名单编辑窗口")
    parser.add_argument("--output", type=Path, default=directory / LICENSE_FILENAME, help="输出的账号授权.json")
    parser.add_argument("--accounts-file", type=Path, default=directory / "accounts.txt", help="图形窗口保存的名单文本")
    parser.add_argument("--self-test", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test is not None:
        # Disposable in-memory key only: the real administrator key is not read.
        key = Ed25519PrivateKey.generate()
        payload = {
            "schema_version": SCHEMA_VERSION, "app_id": APP_ID,
            "license_version": LICENSE_VERSION, "accounts": ["self-test-account"],
        }
        document = {
            "payload": payload,
            "signature": base64.b64encode(key.sign(canonical_payload_bytes(payload))).decode("ascii"),
        }
        public_key = base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode("ascii")
        verify_document(document, "self-test-account", public_key)
        args.self_test.write_text("license-admin-ok", encoding="utf-8")
        return 0
    if args.accounts is None:
        return run_editor(args.key, args.output, args.accounts_file)
    try:
        accounts = parse_accounts(args.accounts)
        sign_license(args.key, accounts, args.output)
    except (AuthorizationError, OSError) as exc:
        print(f"签发失败：{exc}", file=sys.stderr)
        return 1
    print(f"已签发 {len(accounts)} 个账号：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
