# -*- coding: utf-8 -*-
"""会议室自动抢订：可重复使用的 Windows EXE 构建器。

无需每次修改本文件。运行「一键打包EXE.cmd」后选择 F5/F5-1A 或 H7。
两套区域资料由 deployment_profiles.py 维护，选中资料编译进对应EXE。
打包前会从用户的「会议室预定.py」重新生成 booking_core.py，防止打进旧代码。

运行：
    python build_exe.py --check       # 查看实际源码及检查语法（不安装依赖，不打包）
    python build_exe.py --sync-only   # 只同步最新预约核心
    python build_exe.py --profile H7  # Windows 上构建 H7、再自检
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
BUILD_ENV = ROOT / ".packaging_env"


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
        + "RUNTIME_SHA256 = " + repr(runtime_module_hashes(ROOT)) + "\n", encoding="utf-8")
    out(f"[业务源码] {core}")
    out(f"[源码SHA256] {sha256(core)}")
    out(f"[GUI入口] {gui}")
    out(f"[同步] 最新预约源码已同步到 {GENERATED_CORE.name}（自动生成，请勿手动编辑）")
    return core, gui


RUNTIME_MODULES = (
    "account_auth.py", "account_public_key.py", "gui_diagnostics.py",
    "window_chrome.py", "date_picker.py", "w3_credentials.py", "w3_login.py",
)


def runtime_module_hashes(directory: Path) -> dict[str, str]:
    """只读取显式列出的源码模块，不读取本机 W3 凭据或授权文件。"""
    return {Path(filename).stem: sha256(directory / filename) for filename in RUNTIME_MODULES}


def prepare_build_sources(settings: dict, source: Path, gui: Path, profile: dict) -> tuple[Path, Path]:
    """为每套区域生成独立构建输入；只复制运行必需文件，不复制管理员资料。"""
    profile_text = render_profile(profile)  # 写入任何文件前先完成校验
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
    (stage / "booking_profile.py").write_text(profile_text, encoding="utf-8")
    (stage / "build_info.py").write_text(
        "# Auto generated by build_exe.py; do not edit.\n"
        + "CORE_SHA256 = " + repr(sha256(source)) + "\n"
        + "DEPLOYMENT_ID = " + repr(profile["id"]) + "\n"
        + "PROFILE_SHA256 = " + repr(sha256(stage / "booking_profile.py")) + "\n"
        + "RUNTIME_SHA256 = " + repr(runtime_module_hashes(stage)) + "\n", encoding="utf-8")
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
    out("  Q. 取消")
    while True:
        try:
            value = input("请输入编号：").strip()
        except EOFError as exc:
            raise RuntimeError("当前无法交互选择，请使用 --profile F5 或 --profile H7") from exc
        if value.lower() == "q":
            raise KeyboardInterrupt
        if value in choices:
            return value
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1]
        out("输入无效，请选择显示的编号；不会自动打包其他区域。")


def python_command(python: Path, code: str, *, timeout: int = 15) -> tuple[bool, str]:
    try:
        p = subprocess.run([str(python), "-c", code], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, text=True,
                           errors="replace")
        return (p.returncode == 0, p.stdout.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        return (False, f"{type(e).__name__}: {e}")


def tkinter_ok(python: Path) -> bool:
    script = (
        "import tkinter as t; "
        "root=t.Tk(); root.withdraw(); root.update_idletasks(); root.destroy(); "
        "print('Tkinter OK')"
    )
    return python_command(python, script, timeout=20)[0]


def deps_ok(python: Path) -> bool:
    return python_command(python,
        "import playwright.async_api, PyInstaller; print('Build dependencies OK')", timeout=20)[0]


def candidate_pythons():
    items: list[Path] = []
    win_executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    items.append(BUILD_ENV / win_executable)
    items.append(Path(sys.executable))
    items.append(Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3"))
    for ancestor in [ROOT, *ROOT.parents[:4]]:
        items.append(ancestor / ".venv" / win_executable)
    if os.name == "nt":
        try:
            proc = subprocess.run(["py", "-0p"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=10)
            for match in re.findall(r"[A-Za-z]:\\[^\r\n]*?python(?:w)?\.exe", proc.stdout, flags=re.I):
                items.append(Path(match.strip().strip('"')))
        except (OSError, subprocess.TimeoutExpired):
            pass
    seen = set()
    for item in items:
        key = str(item).lower()
        if key not in seen and item.is_file():
            seen.add(key)
            yield item


def create_build_environment(base_python: Path) -> Path:
    exe = BUILD_ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if exe.exists() and tkinter_ok(exe):
        return exe
    if BUILD_ENV.exists():
        out("[环境] 删除损坏的专用构建环境并重新建立")
        shutil.rmtree(BUILD_ENV)
    out(f"[环境] 使用 {base_python} 创建独立打包环境")
    subprocess.run([str(base_python), "-m", "venv", str(BUILD_ENV)], check=True)
    if not tkinter_ok(exe):
        raise RuntimeError("专用打包环境中 tkinter/Tcl 不可用，请在 Python 安装程序中启用 tcl/tk and IDLE")
    return exe


def select_build_python() -> Path:
    candidates = list(candidate_pythons())
    out(f"[环境] 检测Python解释器：{len(candidates)}个候选")
    suitable: list[Path] = []
    build_env_python = (BUILD_ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).resolve()
    for candidate in candidates:
        if tkinter_ok(candidate):
            if deps_ok(candidate):
                out(f"[环境] 复用已具备Tk/Tcl、Playwright、PyInstaller的Python：{candidate}")
                return candidate
            # .packaging_env 若缺依赖会被下面的 create_build_environment 删除重建，
            # 不能把它自己作为新环境的基础解释器。
            try:
                is_build_env = candidate.resolve() == build_env_python
            except OSError:
                is_build_env = False
            if not is_build_env:
                suitable.append(candidate)
    if not suitable:
        raise RuntimeError("未找到能启动 tkinter 窗口的 Python。请在完整的 Windows Python 安装器中选择 Modify -> tcl/tk and IDLE，再重新打包。不要执行 pip install tkinter。")
    # 不往用户系统Python中强装包，专门创建可复用的打包环境。
    return create_build_environment(suitable[0])


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
    cmd = [str(python), "-m", "PyInstaller", "--noconfirm",
           "--onefile", "--windowed", "--noupx", "--name", exe_name,
           "--paths", str(source.parent), "--distpath", str(dist),
           "--workpath", str(ROOT / "build" / "pyinstaller" / profile_id),
           "--specpath", str(ROOT / "build" / "specs" / profile_id)]
    if settings["clean_build"]:
        cmd.append("--clean")
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
        try:
            result = subprocess.run([str(exe), "--self-test", str(marker)], timeout=100)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("EXE启动自检超时：可能是系统安全软件阻止或程序启动卡住") from exc
        if result.returncode != 0 or not marker.is_file():
            raise RuntimeError(f"EXE启动自检失败（退出码{result.returncode}）。请检查打包Python是否有Tk/Tcl；该EXE请勿分发")
        if marker.read_text(encoding="utf-8") != "gui-core-config-ok":
            raise RuntimeError("EXE自检标记无效，请勿分发")


def main() -> int:
    parser = argparse.ArgumentParser(description="会议室自动抢订可复用Windows EXE打包工具")
    parser.add_argument("--check", action="store_true", help="只做源码定位与语法检查，不打包")
    parser.add_argument("--sync-only", action="store_true", help="只同步最新会议室预定.py到booking_core.py")
    parser.add_argument("--profile", choices=tuple(PROFILES), help="打包区域：F5包含F5/F5-1A，H7包含H7")
    args = parser.parse_args()
    profile_id = args.profile or ("F5" if args.check or args.sync_only else choose_profile())
    profile = get_profile(profile_id)
    settings = load_settings()
    settings["profile_id"] = profile_id
    source, gui = sync_sources(settings)
    staged_source, staged_gui = prepare_build_sources(settings, source, gui, profile)
    if args.check or args.sync_only:
        out(f"[成功] {profile['label']} 配置、源码同步及语法检查通过；未执行EXE打包")
        return 0
    if os.name != "nt":
        raise RuntimeError("Windows EXE必须在 Windows 10/11 上打包；当前平台只能使用 --check")
    python = select_build_python()
    out(f"[打包Python] {python}")
    ensure_dependencies(python, settings)
    # GUI导入最新的 booking_core；在同一个Python环境中提前验证，避免打包后才发现漏依赖。
    out("[自检] 测试GUI与当前预约核心的模块导入")
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
        raise RuntimeError("界面与预约核心导入失败。请检查工程内会议室预定.py及模块兼容性；新增库请填写extra_dependencies。")
    cmd, exe = build_command(python, settings, staged_gui, staged_source)
    out(f"[目标EXE] {exe}")
    out(f"[源码SHA256] {sha256(source)}（此源码会进入本次EXE）")
    run_packager(cmd, workdir=staged_source.parent)
    # 授权文件由管理员独立分发，构建过程不读取、生成或覆盖任何授权JSON。
    smoke_test(exe)
    subprocess.run([str(python), str(ROOT / "tools" / "verify_embedded.py"),
                    "--profile", profile_id], cwd=ROOT, check=True)
    out("\n============================================================")
    out("[SUCCESS] 已打包并通过启动自检")
    out(f"[EXE] {exe}")
    out(f"[EXE SHA256] {sha256(exe)}")
    out("[提示] Tk启动及模块导入已验证；会议室网页自动操作仍需在目标电脑模拟测试")
    out("============================================================")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        out("\n[取消] 用户中断了打包")
        sys.exit(130)
    except Exception as exc:
        out(f"\n[ERROR] {type(exc).__name__}: {exc}")
        sys.exit(1)
