"""核对分区域 EXE 与该次暂存构建源码，禁止管理员配置进入运行包。"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import marshal
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
# 本工具也可直接从 tools 目录启动，部署名单统一由管理员配置维护。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from deployment_profiles import PROFILES, get_profile

RUNTIME_MODULES = (
    "booking_core", "booking_profile", "account_auth", "account_public_key",
    "gui_diagnostics", "window_chrome", "date_picker", "build_info",
)
FORBIDDEN_MODULES = ("license_admin", "deployment_profiles")


def normalized(value):
    """忽略打包器改写的文件路径/行号，保留执行字节码与常量类型。"""
    if isinstance(value, types.CodeType):
        fields = (
            "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
            "co_stacksize", "co_flags", "co_code", "co_names", "co_varnames",
            "co_freevars", "co_cellvars", "co_exceptiontable", "co_name", "co_qualname",
        )
        return (types.CodeType, tuple(getattr(value, name) for name in fields),
                normalized(value.co_consts))
    if isinstance(value, tuple):
        return (tuple, tuple(normalized(item) for item in value))
    if isinstance(value, frozenset):
        return (frozenset, frozenset(normalized(item) for item in value))
    if isinstance(value, float):
        return (float, value.hex())
    if isinstance(value, complex):
        return (complex, value.real.hex(), value.imag.hex())
    return (type(value), value)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def literal_assignments(path: Path, names: set[str]) -> dict:
    """只读取构建生成的常量，不执行业务模块或管理员代码。"""
    result = {}
    for node in ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    result[target.id] = ast.literal_eval(node.value)
    missing = names - result.keys()
    if missing:
        raise ValueError(f"暂存构建信息缺少字段：{path.name} / {sorted(missing)}")
    return result


def verify_build(exe: Path, stage: Path, gui: Path, profile_id: str) -> dict:
    """实际包只与该区域 stage 比较，绝不拿根目录 F5 配置核验 H7 包。"""
    get_profile(profile_id)
    exe, stage = Path(exe).resolve(), Path(stage).resolve()
    # 调用方即使传入原 GUI 路径，核验对象仍必须是本次暂存副本。
    staged_gui = stage / Path(gui).name
    report_path = exe.parent / "构建校验.json"
    # 新构建核验失败时不能留下旧的“通过”报告。
    report_path.unlink(missing_ok=True)
    for path in (exe, staged_gui, *(stage / f"{name}.py" for name in RUNTIME_MODULES)):
        if not path.is_file():
            raise FileNotFoundError(f"构建校验缺少必要文件：{path}")
    with exe.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError(f"待校验文件不是有效 Windows EXE：{exe}")
    core_hash = digest(stage / "booking_core.py")
    profile_hash = digest(stage / "booking_profile.py")
    info = literal_assignments(stage / "build_info.py", {
        "CORE_SHA256", "DEPLOYMENT_ID", "PROFILE_SHA256",
    })
    profile = literal_assignments(stage / "booking_profile.py", {"PROFILE"})["PROFILE"]
    if not isinstance(profile, dict) or profile.get("id") != profile_id or info["DEPLOYMENT_ID"] != profile_id:
        raise ValueError("暂存配置或构建信息的部署区域与本次请求不一致")
    if info["CORE_SHA256"] != core_hash or info["PROFILE_SHA256"] != profile_hash:
        raise ValueError("暂存构建信息的源码/区域配置 SHA256 与实际文件不一致")

    # 使用调用者选择的打包解释器执行 CLI；普通导入本工具不要求已安装 PyInstaller。
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(exe))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    forbidden = [name for name in pyz.toc if any(
        name == module or name.startswith(module + ".") for module in FORBIDDEN_MODULES)]
    if forbidden:
        raise ValueError(f"EXE 意外包含管理员模块，禁止分发：{forbidden}")
    checked = []
    for module in RUNTIME_MODULES:
        source = stage / f"{module}.py"
        expected = compile(source.read_text(encoding="utf-8-sig"), str(source), "exec", dont_inherit=True)
        actual = pyz.extract(module)
        if not isinstance(actual, types.CodeType) or normalized(actual) != normalized(expected):
            raise ValueError(f"EXE 嵌入模块与暂存源码不一致：{module}")
        checked.append(module)
    actual_gui = marshal.loads(archive.extract(staged_gui.stem))
    expected_gui = compile(staged_gui.read_text(encoding="utf-8-sig"), str(staged_gui), "exec", dont_inherit=True)
    if not isinstance(actual_gui, types.CodeType) or normalized(actual_gui) != normalized(expected_gui):
        raise ValueError("EXE 嵌入 GUI 与本次暂存源码不一致")
    checked.append(staged_gui.stem)

    report = dict(
        successful=True, deployment_id=profile_id,
        embedded_modules_match=checked, manager_module_absent=True,
        deployment_profiles_module_absent=True, authorization_managed_separately=True,
        exe=str(exe), stage=str(stage), exe_bytes=exe.stat().st_size,
        core_sha256=core_hash, profile_sha256=profile_hash, exe_sha256=digest(exe),
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="核对指定区域 EXE 与该次暂存源码")
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    args = parser.parse_args()
    settings = json.loads((ROOT / "打包配置.json").read_text(encoding="utf-8-sig"))
    app_name = settings.get("app_name", "会议室自动抢订")
    if not isinstance(app_name, str) or not app_name or any(char in app_name for char in '\\/:*?"<>|'):
        raise ValueError("程序名称不是有效文件名")
    stage = ROOT / "build" / "region_sources" / args.profile
    exe = ROOT / "dist" / args.profile / f"{app_name}_{args.profile}.exe"
    gui = stage / Path(settings.get("gui_entry", "会议室自动抢订_GUI.py")).name
    report = verify_build(exe, stage, gui, args.profile)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
