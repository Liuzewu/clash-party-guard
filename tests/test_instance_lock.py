from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from clash_guard.instance_lock import InstanceLock


CHILD = r'''
from pathlib import Path
import sys
from clash_guard.instance_lock import InstanceLock, InstanceRunning
try:
    with InstanceLock(sys.argv[1]):
        Path(sys.argv[2]).write_text('ready', encoding='utf-8')
        sys.stdin.read(1)
except InstanceRunning:
    raise SystemExit(73)
'''


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='guard-lock-test-')
        self.root = Path(self.tmp.name)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if child.stdin:
                child.stdin.close()
            if child.stderr:
                child.stderr.close()
        self.tmp.cleanup()

    def hold(self, directory, marker):
        child = subprocess.Popen([sys.executable, '-u', '-c', CHILD, str(directory), str(marker)],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
        self.children.append(child)
        deadline = time.monotonic() + 5
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        if not marker.exists():
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
            error = child.stderr.read().decode('utf-8', errors='replace')
            self.fail('lock holder did not start: ' + error)
        return child

    def try_lock(self, directory, marker):
        return subprocess.run([sys.executable, '-u', '-c', CHILD, str(directory), str(marker)],
                              input=b'x', stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)

    def test_other_process_fails_without_waiting(self):
        state = self.root / 'state'
        self.hold(state, self.root / 'holder')
        started = time.monotonic()
        attempt = self.try_lock(state, self.root / 'contender')
        self.assertEqual(attempt.returncode, 73, attempt.stderr)
        self.assertFalse((self.root / 'contender').exists())
        self.assertLess(time.monotonic() - started, 4)

    def test_process_exit_releases_lock(self):
        state = self.root / 'state'
        child = self.hold(state, self.root / 'holder')
        child.stdin.write(b'x'); child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 0)
        attempt = self.try_lock(state, self.root / 'next')
        self.assertEqual(attempt.returncode, 0, attempt.stderr)

    def test_process_termination_releases_lock(self):
        state = self.root / 'state'
        child = self.hold(state, self.root / 'holder')
        child.kill(); child.wait(timeout=5)
        attempt = self.try_lock(state, self.root / 'next')
        self.assertEqual(attempt.returncode, 0, attempt.stderr)

    def test_separate_state_directories_are_independent(self):
        self.hold(self.root / 'one', self.root / 'holder')
        attempt = self.try_lock(self.root / 'two', self.root / 'other')
        self.assertEqual(attempt.returncode, 0, attempt.stderr)

    def test_exception_releases_lock(self):
        state = self.root / 'state'
        with self.assertRaisesRegex(RuntimeError, 'example'):
            with InstanceLock(state):
                raise RuntimeError('example')
        attempt = self.try_lock(state, self.root / 'after-exception')
        self.assertEqual(attempt.returncode, 0, attempt.stderr)


if __name__ == '__main__':
    unittest.main()
