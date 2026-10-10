"""emu/session.py: what a Session asks longrun.build for.

No firmware: everything Session reads before the build is stubbed, and the
build itself stops the constructor with the flags it was given.
"""
import os
import tempfile
import types
import unittest
from unittest import mock

from emu import session


class Built(Exception):
    pass


def _build_flags(**kw):
    def build(*args, **flags):
        raise Built(flags)
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        img = fh.name
    device = types.SimpleNamespace(intro_unblocks_frame_sem=True, audio=None)
    try:
        with mock.patch.object(session.config, 'firmware', lambda syx: syx), \
                mock.patch.object(session.config, 'main_image', lambda: img), \
                mock.patch.object(session.devices, 'identify',
                                  lambda syx: (device, None)), \
                mock.patch.object(session.symbols, 'resolve',
                                  lambda image: types.SimpleNamespace(frame_sem=None)), \
                mock.patch.object(session, 'saved_ssi0', lambda snap: None), \
                mock.patch.object(session.longrun, 'build', build):
            session.Session('gui.snap', 'fw.syx', **kw)
    except Built as done:
        return done.args[0]
    finally:
        os.unlink(img)
    raise AssertionError('Session did not build')


class FastIdleTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIs(_build_flags()['fast_idle'], False)

    def test_asked_for(self):
        self.assertIs(_build_flags(fast_idle=True)['fast_idle'], True)


if __name__ == '__main__':
    unittest.main()
