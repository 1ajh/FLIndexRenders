import os
import struct
import tempfile
import unittest

import _fixtures as fx
from flp_parser import parse_flp


class TestFlpParser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_samples_title_version(self):
        p = os.path.join(self.dir, 'a.flp')
        fx.make_flp(p, title='My Song', version='20.8.3',
                    samples=[r'C:\Samples\kick.wav', r'%FLStudioFactoryData%\x.wav'])
        info = parse_flp(p)
        self.assertEqual(info.title, 'My Song')
        self.assertEqual(info.version, '20.8.3')
        self.assertIn(r'C:\Samples\kick.wav', info.samples)
        self.assertEqual(info.error, '')

    def test_not_an_flp(self):
        p = os.path.join(self.dir, 'b.flp')
        with open(p, 'wb') as f:
            f.write(b'this is not an flp file at all')
        info = parse_flp(p)
        self.assertTrue(info.error)
        self.assertEqual(info.samples, [])

    def test_missing_file(self):
        info = parse_flp(os.path.join(self.dir, 'nope.flp'))
        self.assertTrue(info.error.startswith('unreadable'))

    def test_event_172_quirk_on_2524(self):
        # On FL >= 25.2.4 the 3-byte event 172 must not desync the walk; a
        # sample event after it must still be found.
        extra = bytes([172, 0x11, 0x22, 0x33]) + fx._event(196, r'D:\late.wav'.encode('utf-16-le'))
        p = os.path.join(self.dir, 'c.flp')
        fx.make_flp(p, version='25.2.4', samples=[r'C:\early.wav'], extra_events=extra)
        info = parse_flp(p)
        self.assertIn(r'C:\early.wav', info.samples)
        self.assertIn(r'D:\late.wav', info.samples)

    def test_plugin_sweep_finds_blob_samples(self):
        # A UTF-16LE audio path embedded in an opaque blob (not via event 196)
        # is recovered by the raw sweep as a plugin sample.
        blob = fx._event(210, r'E:\FPC\snare.wav'.encode('utf-16-le'))
        p = os.path.join(self.dir, 'd.flp')
        fx.make_flp(p, samples=[r'C:\a.wav'], extra_events=blob)
        info = parse_flp(p)
        self.assertIn(r'C:\a.wav', info.samples)
        self.assertTrue(any('snare.wav' in s for s in info.plugin_samples))


if __name__ == '__main__':
    unittest.main()
