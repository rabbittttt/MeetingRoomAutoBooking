"""Run local Chrome fixtures and record exactly which code/tests were checked."""
from pathlib import Path
import contextlib
import hashlib
import json
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


if __name__ == '__main__':
    all_tests = list(flatten(unittest.defaultTestLoader.discover(str(ROOT / 'tests'))))
    selected = [test for test in all_tests if 'BrowserTests' in type(test).__name__
                and 'Pipeline' not in type(test).__name__]
    before = hashlib.sha256((ROOT / 'booking_core.py').read_bytes()).hexdigest()
    started = time.monotonic()
    with (ROOT / '浏览器回归测试.log').open('w', encoding='utf-8') as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.TestSuite(selected))
    report = dict(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  skipped=len(result.skipped), seconds=round(time.monotonic() - started, 3),
                  core_sha256_before=before,
                  core_sha256_after=hashlib.sha256((ROOT / 'booking_core.py').read_bytes()).hexdigest(),
                  included_tests=[test.id() for test in selected])
    (ROOT / '浏览器回归范围.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'included_tests'}, ensure_ascii=False))
    sys.exit(0 if result.wasSuccessful() and before == report['core_sha256_after'] else 1)
