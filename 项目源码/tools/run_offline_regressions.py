"""Run offline regressions separately from browser and desktop window tests."""
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main():
    selected, excluded = [], []
    loader = unittest.TestLoader()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name.startswith("test_window_"):
            excluded.append({"module": path.name, "reason": "window GUI tests run separately"})
            continue
        for test in flatten(loader.loadTestsFromName("tests." + path.stem)):
            if "BrowserTests" in type(test).__name__ or "PipelineBrowserTests" in type(test).__name__:
                excluded.append({"test": test.id(), "reason": "browser tests run separately"})
            else:
                selected.append(test)
    core_file = ROOT / "booking_core.py"
    metadata = {
        "scope": "Offline regression; excludes BrowserTests/PipelineBrowserTests and test_window_*",
        "core_sha256_before": hashlib.sha256(core_file.read_bytes()).hexdigest(),
        "included_tests": [test.id() for test in selected],
        "excluded": excluded,
    }
    with (ROOT / "离线回归测试.log").open("w", encoding="utf-8") as stream:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.TestSuite(selected))
    metadata.update(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                    skipped=len(result.skipped), successful=result.wasSuccessful(),
                    core_sha256_after=hashlib.sha256(core_file.read_bytes()).hexdigest())
    (ROOT / "离线回归范围.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in metadata.items()
                      if key not in ("included_tests", "excluded")}, ensure_ascii=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
