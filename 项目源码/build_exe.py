# -*- coding: utf-8 -*-
"""会议室自动抢订：可重复使用的 Windows EXE 构建器。

无需每次修改本文件。运行「一键打包EXE.cmd」后选择单个区域或全部打包。
各区域资料由 deployment_profiles.py 维护，每套资料分别编译进对应EXE。
打包前会从用户的「会议室预定.py」重新生成 booking_core.py，防止打进旧代码。

运行：
    python build_exe.py --check       # 查看实际源码及检查语法（不安装依赖，不打包）
    python build_exe.py --sync-only   # 只同步最新预约核心
    python build_exe.py --profile H7  # Windows 上构建 H7、再自检
    python build_exe.py --profile all # 依次构建全部区域、逐个自检
    python build_exe.py               # 交互选择本次区域
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from deployment_profiles import PROFILES, get_profile, get_profile_label, render_profile

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "打包配置.json"
DEFAULTS = {
    "app_name": "会议室自动抢订",
    "core_source": "auto",
    "gui_entry": "会议室自动抢订_GUI.py",
    "clean_build": True,
    "extra_dependencies": [],
    "hidden_imports": [],
    "collect_all": ["playwright"],
    "data_files": [],
    "module_paths": [],
}
GENERATED_CORE = ROOT / "booking_core.py"


def out(message: str) -> None:
    print(message, flush=True)


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("打包配置.json 必须是JSON对象")
        unknown = set(raw) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"打包配置.json 有不认识的配置项：{sorted(unknown)}")
        settings.update(raw)
    for field in ("app_name", "core_source", "gui_entry"):
        if not isinstance(settings[field], str) or not settings[field].strip():
            raise ValueError(f"{field} 必须填写非空字符串")
    for field in ("extra_dependencies", "hidden_imports", "collect_all", "data_files", "module_paths"):
        if not isinstance(settings[field], list):
            raise ValueError(f"{field} 必须是列表")
    for field in ("app_name",):
        if re.search(r'[\\/:*?"<>|]', settings[field]):
            raise ValueError(f"{field} 不能包含Windows文件名禁用字符")
    if not isinstance(settings["clean_build"], bool):
        raise ValueError("clean_build 必须是 true 或 false")
    return settings


def resolve_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser() if Path(os.path.expandvars(value)).is_absolute() else (ROOT / os.path.expandvars(value)).expanduser()


def select_core(settings: dict) -> Path:
    configured = settings["core_source"].strip()
    if configured.lower() != "auto":
        path = resolve_path(configured).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"预约源码不存在：{path}。请修改打包配置.json 的 core_source")
        return path
    # 自动选择只使用本工程工作源码，避免父目录中的其他脚本混入本次构建。
    path = ROOT / "会议室预定.py"
    if not path.is_file():
        raise FileNotFoundError(f"预约源码不存在：{path}。请修改打包配置.json 的 core_source")
    return path.resolve()



def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def inspect_python_file(path: Path, must_have: set[str] | None = None) -> None:
    content = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(content, filename=str(path))
    compile(tree, str(path), "exec")
    if must_have:
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        missing = must_have - names
        if missing:
            raise RuntimeError(f"业务源码与界面不兼容，缺少 {sorted(missing)}。"
                               "请检查打包程序实际读取的会议室预定.py；"
                               "或在打包配置.json中将core_source改为'会议室预定.py'以使用工程内源码。")


def sync_sources(settings: dict) -> tuple[Path, Path]:
    core = select_core(settings)
    gui = resolve_path(settings["gui_entry"]).resolve()
    if not gui.is_file():
        raise FileNotFoundError(f"GUI 入口不存在：{gui}")
    inspect_python_file(core, {"apply_gui_settings", "build_release_batches", "parse_targets", "main",
                               "parse_weekly_rules", "expand_weekly_targets", "merge_release_targets",
                               "log_booking_popup_raw", "booking_failure_dialog", "confirm_and_book", "watch_confirm_disabled"})
    inspect_python_file(gui)
    for filename in RUNTIME_MODULES:
        inspect_python_file(ROOT / filename)
    asset_hashes = runtime_asset_hashes(ROOT)
    # 源码运行保持F5默认。正式打包在独立暂存目录生成所选区域，不来回改主脚本。
    if settings.get("profile_id", "F5") == "F5":
        (ROOT / "booking_profile.py").write_text(render_profile(get_profile("F5")), encoding="utf-8")
    # 只同步副本，不写回工作源码；保留UTF-8原始内容，不改用户的文件。
    if core != GENERATED_CORE.resolve():
        raw = core.read_bytes()
        if not GENERATED_CORE.exists() or GENERATED_CORE.read_bytes() != raw:
            GENERATED_CORE.write_bytes(raw)
    # 通过源码哈希识别本次构建，不在程序名称或界面中维护项目版本号。
    build_info_source = ROOT / "build_info.py"
    build_info_source.write_text(
        "# Auto generated by build_exe.py; do not edit.\n"
        + "CORE_SHA256 = " + repr(sha256(core)) + "\n"
        + "RUNTIME_SHA256 = " + repr(runtime_module_hashes(ROOT)) + "\n"
        + "ASSET_SHA256 = " + repr(asset_hashes) + "\n", encoding="utf-8")
    out(f"[业务源码] {core}")
    out(f"[源码SHA256] {sha256(core)}")
    out(f"[GUI入口] {gui}")
    out(f"[同步] 最新预约源码已同步到 {GENERATED_CORE.name}（自动生成，请勿手动编辑）")
    return core, gui


RUNTIME_MODULES = (
    "account_auth.py", "account_public_key.py", "gui_diagnostics.py",
    "window_chrome.py", "date_picker.py", "w3_credentials.py", "w3_login.py",
    "desktop_lifecycle.py",
)
RUNTIME_ASSETS = (
    "assets/app_icon.ico",
    "assets/title_icon_24.png", "assets/title_icon_30.png",
    "assets/title_icon_36.png", "assets/title_icon_48.png",
)


def runtime_module_hashes(directory: Path) -> dict[str, str]:
    """只读取显式列出的源码模块，不读取本机 W3 凭据或授权文件。"""
    return {Path(filename).stem: sha256(directory / filename) for filename in RUNTIME_MODULES}


def runtime_asset_hashes(directory: Path) -> dict[str, str]:
    """只核对明确列出的运行素材，不扫描或收集本机其他资料。"""
    for filename in RUNTIME_ASSETS:
        if not (directory / filename).is_file():
            raise FileNotFoundError(f"缺少程序图标素材：{directory / filename}")
    return {filename: sha256(directory / filename) for filename in RUNTIME_ASSETS}


def prepare_build_sources(settings: dict, source: Path, gui: Path, profile: dict) -> tuple[Path, Path]:
    """为每套区域生成独立构建输入；只复制运行必需文件，不复制管理员资料。"""
    profile_text = render_profile(profile)  # 写入任何文件前先完成校验
    runtime_asset_hashes(ROOT)
    stage = ROOT / "build" / "region_sources" / profile["id"]
    stage.mkdir(parents=True, exist_ok=True)
    staged_source = stage / "booking_core.py"
    staged_source.write_bytes(source.read_bytes())
    staged_gui = stage / gui.name
    staged_gui.write_bytes(gui.read_bytes())
    for filename in RUNTIME_MODULES:
        original = ROOT / filename
        inspect_python_file(original)
        (stage / filename).write_bytes(original.read_bytes())
    for filename in RUNTIME_ASSETS:
        destination = stage / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / filename).read_bytes())
    (stage / "booking_profile.py").write_text(profile_text, encoding="utf-8")
    (stage / "build_info.py").write_text(
        "# Auto generated by build_exe.py; do not edit.\n"
        + "CORE_SHA256 = " + repr(sha256(source)) + "\n"
        + "DEPLOYMENT_ID = " + repr(profile["id"]) + "\n"
        + "PROFILE_SHA256 = " + repr(sha256(stage / "booking_profile.py")) + "\n"
        + "RUNTIME_SHA256 = " + repr(runtime_module_hashes(stage)) + "\n"
        + "ASSET_SHA256 = " + repr(runtime_asset_hashes(stage)) + "\n", encoding="utf-8")
    for path in stage.glob("*.py"):
        inspect_python_file(path)
    out(f"[打包区域] {profile['label']}｜默认放号={profile['release_open_at']}｜"
        f"房间={sum(len(area['rooms']) for area in profile['room_catalog'].values())}间")
    return staged_source, staged_gui


def choose_profile() -> str:
    """无参数启动时让管理员选区域；取消或输错不得默默构建默认版。"""
    choices = tuple(PROFILES)
    out("\n请选择本次打包区域：")
    for index, profile_id in enumerate(choices, 1):
        out(f"  {index}. {get_profile_label(profile_id)}")
    out(f"  {len(choices) + 1}. 全部打包（{len(choices)}套区域）")
    out("  Q. 取消")
    while True:
        try:
            value = input("请输入编号：").strip()
        except EOFError as exc:
            raise RuntimeError("当前无法交互选择，请使用 --profile 区域编号 或 --profile all") from exc
        if value.lower() == "q":
            raise KeyboardInterrupt
        if value in choices:
            return value
        if value.lower() == "all" or value == str(len(choices) + 1):
            return "all"
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1]
        out("输入无效，请选择显示的编号。")


def python_command(python: Path, code: str, *, timeout: int = 15) -> tuple[bool, str]:
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([str(python), "-c", code], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, encoding="utf-8",
                           errors="replace", env=env)
        return (p.returncode == 0, p.stdout.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        return (False, f"{type(e).__name__}: {e}")


def check_tkinter(python: Path) -> tuple[bool, str]:
    script = (
        "import sys; print('Python:', sys.executable, flush=True); "
        "print('Base Python:', sys.base_prefix, flush=True); "
        "print('Version:', sys.version.split()[0], flush=True); "
        "import tkinter as t; "
        "root=t.Tk(); root.withdraw(); root.update_idletasks(); root.destroy(); "
        "print('Tkinter OK')"
    )
    return python_command(python, script, timeout=20)


def recover_tkinter_paths(python: Path) -> tuple[bool, str]:
    """兼容Windows旧版venv的Tcl路径发现问题，只改变当前构建进程环境。"""
    ok, raw = python_command(python,
        "import sys,json,_tkinter; print(json.dumps({'base':sys.base_prefix,"
        "'tcl':_tkinter.TCL_VERSION,'tk':_tkinter.TK_VERSION}))")
    if not ok:
        return False, raw
    try:
        info = json.loads(raw)
        base = Path(info["base"])
        if not base.is_absolute() or any(re.fullmatch(r"\d+\.\d+", info[key]) is None
                                         for key in ("tcl", "tk")):
            raise ValueError("无法核验基础Python的Tcl/Tk信息")
        libraries = {"TCL_LIBRARY": base / "tcl" / ("tcl" + info["tcl"]),
                     "TK_LIBRARY": base / "tcl" / ("tk" + info["tk"])}
        required = (libraries["TCL_LIBRARY"] / "init.tcl", libraries["TK_LIBRARY"] / "tk.tcl")
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            return False, "基础Python缺少以下组件文件，请用其安装程序修复 tcl/tk and IDLE：\n" + "\n".join(missing)
    except (KeyError, TypeError, ValueError) as exc:
        return False, f"无法读取基础Python的Tcl/Tk目录：{exc}"
    previous = {key: os.environ.get(key) for key in libraries}
    recovered = False
    try:
        os.environ.update({key: str(path) for key, path in libraries.items()})
        recovered, detail = check_tkinter(python)
        return recovered, ("\n".join(f"{key}={path}" for key, path in libraries.items())
                           + "\n" + detail)
    finally:
        if not recovered:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def deps_ok(python: Path) -> bool:
    return python_command(python,
        "import playwright.async_api, PyInstaller; print('Build dependencies OK')", timeout=20)[0]


def candidate_pythons():
    configured = os.environ.get("BOOKING_PYTHON")
    if configured:
        executable = Path(os.path.expandvars(configured)).expanduser()
        if not executable.is_absolute():
            executable = ROOT / executable
    else:
        workspace = next((p for p in (ROOT, *ROOT.parents) if p.name.lower() == "project"), None)
        if workspace is None:
            raise RuntimeError("找不到 Project 工作区。请保持项目位于 Project 内，并初始化共享 .venv。")
        executable = workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        raise RuntimeError(f"共享 Python 不存在：{executable}。请初始化 Project/.venv。")
    yield executable.resolve()



def select_build_python() -> Path:
    candidate = next(candidate_pythons())
    ok, detail = check_tkinter(candidate)
    if not ok and sys.platform == "win32" and "can't find a usable init.tcl" in detail.lower():
        ok, recovery = recover_tkinter_paths(candidate)
        if ok:
            out("[Tk兼容] 已核验并使用基础Python的Tcl/Tk目录，仅本次打包生效")
            out(recovery)
        else:
            detail += "\n[Tcl/Tk目录检查]\n" + recovery
    if not ok:
        raise RuntimeError(f"共享 Python 的 Tk 窗口自检失败：{candidate}\n"
                           f"[Tk自检原始输出]\n{detail or '子进程未返回诊断文字'}\n"
                           "请根据原始错误检查此电脑的基础 Python/Tcl/Tk；不要 pip install tkinter。")
    out(f"[环境] 使用 Project 共享 Python：{candidate}")
    return candidate


def modules_missing(python: Path, modules: list[str]) -> list[str]:
    result = []
    for module in modules:
        if not python_command(python, f"import importlib; importlib.import_module({module!r})", timeout=15)[0]:
            result.append(module)
    return result


def ensure_dependencies(python: Path, settings: dict) -> None:
    # 可以在打包配置.json 中加入新的 pip 包，不需要更改打包工具本身。
    requirements = [("PyInstaller", "pyinstaller>=6.12"), ("playwright", "playwright>=1.51")]
    for item in settings["extra_dependencies"]:
        if isinstance(item, str):
            requirements.append((item, item))
        elif isinstance(item, dict) and set(item) == {"module", "pip"}:
            requirements.append((item["module"], item["pip"]))
        else:
            raise ValueError("extra_dependencies 中每项须为包名字符串，或{module,pip}对象")
    for module, requirement in requirements:
        if not modules_missing(python, [module]):
            continue
        out(f"[依赖] 安装 {requirement} ...")
        subprocess.run([str(python), "-m", "pip", "install", requirement], check=True)
        if modules_missing(python, [module]):
            raise RuntimeError(f"已尝试安装 {requirement}，但仍无法导入 {module}，请检查网络、代理和Python版本")
    # 代码使用 Locator.filter(visible=...)，要求 Playwright >= 1.51。
    ok_pw, pw_ver = python_command(
        python, "from importlib.metadata import version;print(version('playwright'))"
    )
    pw_parts = tuple(map(int, re.findall(r"\d+", pw_ver)[:2])) if ok_pw else ()
    if not pw_parts or pw_parts < (1, 51):
        out("[依赖] Playwright版本过低，更新到1.51+")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", "playwright>=1.51"],
            check=True,
        )
        if not modules_missing(python, ["playwright"]):
            ok_pw2, pw_ver2 = python_command(
                python, "from importlib.metadata import version;print(version('playwright'))"
            )
            pw_parts2 = tuple(map(int, re.findall(r"\d+", pw_ver2)[:2])) if ok_pw2 else ()
            if not pw_parts2 or pw_parts2 < (1, 51):
                raise RuntimeError(f"Playwright升级后版本仍不满足>=1.51：{pw_ver2}")
    # 6.12+ 兼容Python3.13，若旧环境太老则升级。
    ok_py, builder_ver = python_command(python, "import sys;print('%d.%d' % sys.version_info[:2])")
    builder_parts = tuple(map(int, builder_ver.split('.'))) if ok_py and re.fullmatch(r'\d+\.\d+', builder_ver) else ()
    if builder_parts >= (3, 13):
        ok, ver = python_command(python, "from importlib.metadata import version;print(version('pyinstaller'))")
        numbers = tuple(map(int, re.findall(r"\d+", ver)[:2])) if ok else ()
        if numbers and numbers < (6, 12):
            out("[依赖] PyInstaller版本偏旧，更新到6.12+以支持Python3.13")
            subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pyinstaller>=6.12"], check=True)


def build_command(python: Path, settings: dict, gui: Path, source: Path) -> tuple[list[str], Path]:
    profile_id = settings["profile_id"]
    exe_name = f"{settings['app_name']}_{profile_id}"
    dist = ROOT / "dist" / profile_id
    runtime_asset_hashes(source.parent)
    icon = source.parent / "assets" / "app_icon.ico"
    cmd = [str(python), "-m", "PyInstaller", "--noconfirm",
           "--onefile", "--windowed", "--noupx", "--name", exe_name,
           "--icon", str(icon),
           "--paths", str(source.parent), "--distpath", str(dist),
           "--workpath", str(ROOT / "build" / "pyinstaller" / profile_id),
           "--specpath", str(ROOT / "build" / "specs" / profile_id)]
    if settings["clean_build"]:
        cmd.append("--clean")
    for filename in RUNTIME_ASSETS:
        asset = source.parent / filename
        cmd.extend(["--add-data", f"{asset}{os.pathsep}{Path(filename).parent.as_posix()}"])
    for module_path in settings["module_paths"]:
        if not isinstance(module_path, str) or not module_path.strip():
            raise ValueError("module_paths 中必须填写目录路径")
        path = resolve_path(module_path).resolve()
        if not path.is_dir():
            raise NotADirectoryError(f"找不到新增Python模块目录：{path}")
        cmd.extend(["--paths", str(path)])
    for name in ["booking_core", "booking_profile", "tkinter", "_tkinter",
                 *(Path(filename).stem for filename in RUNTIME_MODULES), *settings["hidden_imports"]]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("hidden_imports 中必须使用非空模块名")
        cmd.extend(["--hidden-import", name])
    for package in settings["collect_all"]:
        if not isinstance(package, str) or not package.strip():
            raise ValueError("collect_all 中必须使用非空包名")
        cmd.extend(["--collect-all", package])
    for item in settings["data_files"]:
        if not isinstance(item, dict) or set(item) != {"source", "dest"}:
            raise ValueError("data_files 中每项须有 source 和 dest")
        src = resolve_path(str(item["source"])).resolve()
        if not src.exists():
            raise FileNotFoundError(f"待打包数据文件不存在：{src}")
        # 只查文件名，禁止将本机加密凭据作为数据打包；不读取其内容。
        if (src.name.casefold() == "w3_credentials.dat" or src.name.casefold().startswith("w3_credentials.dat.") or
                (src.is_dir() and any(path.name.casefold() == "w3_credentials.dat" or
                                      path.name.casefold().startswith("w3_credentials.dat.")
                                      for path in src.rglob("*")))):
            raise ValueError("W3 本机凭据 w3_credentials.dat 不得加入程序包")
        dest = str(item["dest"])
        if not dest or Path(dest).is_absolute() or ".." in Path(dest).parts:
            raise ValueError("数据文件目标目录必须是包内部的相对路径")
        cmd.extend(["--add-data", f"{src}{os.pathsep}{dest}"])
    cmd.append(str(gui))
    return cmd, dist / (exe_name + ".exe")


def run_packager(cmd: list[str], *, workdir: Path) -> None:
    out("[打包] 开始执行PyInstaller（日志会同时写到打包日志）")
    logs = ROOT / "打包日志"
    logs.mkdir(exist_ok=True)
    log_path = logs / ("build_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
    out(f"[打包日志] {log_path}")
    with log_path.open("w", encoding="utf-8") as log:
        p = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1)
        try:
            assert p.stdout is not None
            for line in p.stdout:
                print(line, end="", flush=True)
                log.write(line)
        finally:
            rc = p.wait()
        if rc:
            raise RuntimeError(f"PyInstaller打包失败（退出码{rc}）。请查看打包日志：{log_path}")


def smoke_test(exe: Path) -> None:
    if not exe.is_file() or exe.stat().st_size < 100_000:
        raise RuntimeError(f"没有生成有效的Windows可执行文件：{exe}")
    with exe.open("rb") as f:
        if f.read(2) != b"MZ":
            raise RuntimeError("EXE文件头错误，生成物不是Windows程序")
    with tempfile.TemporaryDirectory(prefix="meeting_build_smoke_") as folder:
        marker = Path(folder) / "tk_result.txt"
        out("[自检] 启动新EXE进行中文GUI、Tk/Tcl及预约参数映射自检")
        env = os.environ.copy()
        # 新EXE必须使用自己的内嵌组件，不能借用本次构建临时指定的安装目录。
        env.pop("TCL_LIBRARY", None)
        env.pop("TK_LIBRARY", None)
        try:
            result = subprocess.run([str(exe), "--self-test", str(marker)], timeout=100, env=env)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("EXE启动自检超时：可能是系统安全软件阻止或程序启动卡住") from exc
        if result.returncode != 0 or not marker.is_file():
            raise RuntimeError(f"EXE启动自检失败（退出码{result.returncode}）。请检查打包Python是否有Tk/Tcl；该EXE请勿分发")
        if marker.read_text(encoding="utf-8") != "gui-core-config-ok":
            raise RuntimeError("EXE自检标记无效，请勿分发")


def build_from_args() -> int:
    parser = argparse.ArgumentParser(description="会议室自动抢订可复用Windows EXE打包工具")
    parser.add_argument("--check", action="store_true", help="只做源码定位与语法检查，不打包")
    parser.add_argument("--sync-only", action="store_true", help="只同步最新会议室预定.py到booking_core.py")
    parser.add_argument("--profile", choices=(*PROFILES, "all"), help="打包区域编号，或 all 打包全部区域")
    args = parser.parse_args()
    selection = args.profile or ("F5" if args.check or args.sync_only else choose_profile())
    profile_ids = tuple(PROFILES) if selection == "all" else (selection,)
    profiles = [get_profile(profile_id) for profile_id in profile_ids]
    settings = load_settings()
    settings["profile_id"] = "F5" if selection == "all" else selection
    source, gui = sync_sources(settings)
    staged = {profile["id"]: prepare_build_sources(settings, source, gui, profile)
              for profile in profiles}
    if args.check or args.sync_only:
        out(f"[成功] {', '.join(profile['label'] for profile in profiles)} 配置、源码同步及语法检查通过；未执行EXE打包")
        return 0
    if os.name != "nt":
        raise RuntimeError("Windows EXE必须在 Windows 10/11 上打包；当前平台只能使用 --check")
    python = select_build_python()
    out(f"[打包Python] {python}")
    ensure_dependencies(python, settings)
    # 先核验所有区域的模块导入，避免全部打包时前几套已成功、最后一套才发现配置错误。
    for profile in profiles:
        profile_id = profile["id"]
        staged_source, staged_gui = staged[profile_id]
        out(f"[自检] 测试 {profile['label']} 的GUI与预约核心模块导入")
        env = os.environ.copy()
        extra_paths = [str(staged_source.parent), *(str(resolve_path(value).resolve()) for value in settings["module_paths"])]
        env["PYTHONPATH"] = os.pathsep.join(extra_paths)
        check_code = ("import importlib; import booking_core as c; import tkinter; "
                      f"g=importlib.import_module({staged_gui.stem!r}); "
                      f"assert c.DEPLOYMENT_ID == {profile_id!r}; "
                      f"assert c.RELEASE_OPEN_AT == g.DEFAULT_CONFIG['release_open_at'] == {profile['release_open_at']!r}; "
                      f"assert c.ROOM_CATALOG == {profile['room_catalog']!r}; "
                      "print('GUI/Core/Profile/Tk import OK')")
        check = subprocess.run([str(python), "-B", "-c", check_code], cwd=staged_source.parent, env=env)
        if check.returncode:
            raise RuntimeError(f"{profile['label']} 界面与预约核心导入失败。请检查源码及模块兼容性；新增库请填写extra_dependencies。")
    outputs = []
    for index, profile in enumerate(profiles, 1):
        profile_id = profile["id"]
        staged_source, staged_gui = staged[profile_id]
        region_settings = dict(settings, profile_id=profile_id)
        out(f"\n[打包进度] {index}/{len(profiles)}：{profile['label']}")
        cmd, exe = build_command(python, region_settings, staged_gui, staged_source)
        out(f"[目标EXE] {exe}")
        out(f"[源码SHA256] {sha256(source)}（此源码会进入本次EXE）")
        run_packager(cmd, workdir=staged_source.parent)
        # 授权文件由管理员独立分发，构建过程不读取、生成或覆盖任何授权JSON。
        smoke_test(exe)
        subprocess.run([str(python), str(ROOT / "tools" / "verify_embedded.py"),
                        "--profile", profile_id], cwd=ROOT, check=True)
        outputs.append(exe)
        out(f"[完成] {profile['label']} 已打包并通过启动、内嵌代码自检")
    out("\n============================================================")
    out(f"[SUCCESS] 已打包并通过自检：{len(outputs)}套区域")
    for exe in outputs:
        out(f"[EXE] {exe}")
        out(f"[EXE SHA256] {sha256(exe)}")
    out("[提示] Tk启动及模块导入已验证；会议室网页自动操作仍需在目标电脑模拟测试")
    out("============================================================")
    return 0


def main() -> int:
    previous = {key: os.environ.get(key) for key in ("TCL_LIBRARY", "TK_LIBRARY")}
    try:
        return build_from_args()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        out("\n[取消] 用户中断了打包")
        sys.exit(130)
    except Exception as exc:
        out(f"\n[ERROR] {type(exc).__name__}: {exc}")
        sys.exit(1)
