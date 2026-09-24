"""tools/ci/release_version.py: the version the release workflow builds as.

The app's own version is emu/portable.py's APP_VERSION (the launcher title,
the firmware folders' stamps), so a release tag has to match it exactly.
No firmware, no network: the checks run on the real portable.py and on
stand-in trees in a temp directory.
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'release_version', os.path.join(REPO, 'tools', 'ci', 'release_version.py'))
rv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rv)


def _tree(d, line):
    os.makedirs(os.path.join(d, 'emu'))
    with open(os.path.join(d, 'emu', 'portable.py'), 'w', encoding='utf-8') as fh:
        fh.write("APP_NAME = 'digiemu'\n%s\n" % line)
    return d


def _run(argv, root):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = rv.main(argv, root=root)
    return rc, out.getvalue(), err.getvalue()


class ReleaseVersionTest(unittest.TestCase):
    def test_reads_the_real_app_version(self):
        from emu import portable
        self.assertEqual(rv.app_version(), portable.APP_VERSION)

    def test_a_matching_tag_passes(self):
        self.assertIsNone(rv.check_tag('v1.2.3', '1.2.3'))

    def test_a_tag_for_another_version_is_refused(self):
        problem = rv.check_tag('v0.2.0', '0.1.0')
        self.assertIn('does not match APP_VERSION 0.1.0', problem)
        self.assertIn('bump APP_VERSION to 0.2.0', problem)

    def test_only_vx_y_z_is_a_release_tag(self):
        for tag in ('0.1.0', 'v0.1', 'v0.1.0-rc1', 'v0.1.0.1', 'release-0.1.0', '', None):
            self.assertIn('not a release tag', rv.check_tag(tag, '0.1.0'), tag)

    def test_main_prints_the_version_or_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = _tree(d, "APP_VERSION = '4.5.6'")
            self.assertEqual(_run([], root), (0, '4.5.6\n', ''))
            self.assertEqual(_run(['--tag', 'v4.5.6'], root)[:2], (0, '4.5.6\n'))
            rc, out, err = _run(['--tag', 'v4.5.7'], root)
            self.assertEqual((rc, out), (1, ''))
            self.assertIn('does not match', err)

    def test_an_unreadable_app_version_fails(self):
        for line in ("APP_VERSION = 'dev'", "VERSION = '1.0.0'"):
            with tempfile.TemporaryDirectory() as d:
                rc, out, err = _run([], _tree(d, line))
                self.assertEqual((rc, out), (1, ''), line)
                self.assertIn('APP_VERSION', err)


class PatchCheckoutTest(unittest.TestCase):
    """The Unicorn patches are pinned by SHA-256, so git must never convert
    their line endings: the release runner, like Git for Windows by default,
    has core.autocrlf=true, and that broke the first Windows build."""

    def test_patches_are_not_text(self):
        import glob
        import shutil
        import subprocess
        if shutil.which('git') is None or not os.path.isdir(os.path.join(REPO, '.git')):
            self.skipTest('not a git checkout')
        patches = sorted(glob.glob(os.path.join(REPO, 'patches', '*.patch')))
        self.assertTrue(patches)
        out = subprocess.run(['git', 'check-attr', 'text', '--'] + patches, cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            self.assertTrue(line.endswith(': text: unset'), line)


if __name__ == '__main__':
    unittest.main()
