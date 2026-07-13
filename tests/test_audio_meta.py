import os
import struct
import tempfile
import unittest
import wave

import _fixtures as fx
import audio_meta as am


class TestAudioMeta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_wav_duration(self):
        p = os.path.join(self.dir, 'r.wav')
        with wave.open(p, 'wb') as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b'\x00\x00\x00\x00' * 44100)  # 1.0s
        a = am.probe(p)
        self.assertEqual(a.codec, 'wav')
        self.assertAlmostEqual(a.duration, 1.0, places=2)
        self.assertEqual(a.sample_rate, 44100)
        self.assertEqual(a.channels, 2)
        self.assertEqual(a.bit_depth, 16)

    def test_declared_size_wav(self):
        p = fx.make_wav(os.path.join(self.dir, 'big.wav'), 183.5)
        a = am.probe(p)
        self.assertAlmostEqual(a.duration, 183.5, places=1)

    def test_acid_and_bext_flags(self):
        p = fx.make_wav(os.path.join(self.dir, 'a.wav'), 200,
                        extra_chunks=fx.acid_chunk(174.0))
        a = am.probe(p)
        self.assertTrue(a.has_acid)
        self.assertAlmostEqual(a.acid_tempo, 174.0, places=1)
        self.assertFalse(a.has_bext)

        p2 = fx.make_wav(os.path.join(self.dir, 'b.wav'), 200,
                         extra_chunks=fx.bext_chunk('Pro Tools'))
        a2 = am.probe(p2)
        self.assertTrue(a2.has_bext)

    def test_aiff_duration(self):
        p = fx.make_aiff(os.path.join(self.dir, 's.aiff'), 2.0, sr=48000)
        a = am.probe(p)
        self.assertEqual(a.codec, 'aiff')
        self.assertAlmostEqual(a.duration, 2.0, places=2)
        self.assertEqual(a.sample_rate, 48000)

    def test_extended80_roundtrip(self):
        for rate in (44100, 48000, 88200, 96000, 22050):
            self.assertAlmostEqual(am._extended80_to_double(fx._ext80(rate)),
                                   rate, places=1)

    def test_mp3_header_parse(self):
        # FF FB 90 64 => MPEG1 Layer III, 128 kbps, 44100 Hz, stereo
        parsed = am._parse_mp3_header(bytes([0xFF, 0xFB, 0x90, 0x64]))
        self.assertIsNotNone(parsed)
        ver, layer, bitrate, srate, padding, channels = parsed
        self.assertEqual((ver, layer, bitrate, srate), (3, 3, 128, 44100))

    def test_unrecognized(self):
        p = os.path.join(self.dir, 'x.bin')
        with open(p, 'wb') as f:
            f.write(b'not audio' * 20)
        a = am.probe(p)
        self.assertTrue(a.error)
        self.assertIsNone(a.duration)

    def test_never_raises_on_truncated(self):
        p = os.path.join(self.dir, 't.wav')
        with open(p, 'wb') as f:
            f.write(b'RIFF\x04\x00\x00\x00WA')  # garbage/truncated
        a = am.probe(p)          # must not raise
        self.assertIsInstance(a, am.AudioInfo)

    def test_never_raises_on_one_byte_ff(self):
        # Regression: a 1-byte 0xFF file used to reach magic[1] and raise
        # IndexError out of probe(), violating the never-raises contract.
        p = os.path.join(self.dir, 'one.mp3')
        with open(p, 'wb') as f:
            f.write(b'\xff')
        a = am.probe(p)
        self.assertIsInstance(a, am.AudioInfo)
        self.assertIsNone(a.duration)

    def test_bext_originator_detected(self):
        # Regression: Originator was read from offset 352 instead of 256, so an
        # FL/software name in the bext chunk was never seen.
        p = fx.make_wav(os.path.join(self.dir, 'bw.wav'), 200,
                        extra_chunks=fx.bext_chunk('FL Studio 21'))
        a = am.probe(p)
        self.assertTrue(a.has_bext)
        self.assertIn('FL Studio', a.encoder)
        self.assertTrue(a.fl_signature)


if __name__ == '__main__':
    unittest.main()
