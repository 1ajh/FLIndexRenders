"""Helpers to synthesize tiny FLP and audio files for the test suite.

The WAV builder declares a `data` chunk size without writing the audio bytes —
audio_meta computes duration from the header, so this keeps fixtures a few dozen
bytes each while letting a test pin an exact duration.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _event(eid, payload):
    n = len(payload)
    vb = b''
    while True:
        b = n & 0x7F
        n >>= 7
        vb += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            break
    return bytes([eid]) + vb + payload


def make_flp(path, title='', version='20.8.3', samples=(), extra_events=b''):
    header = b'FLhd' + struct.pack('<I', 6) + struct.pack('<HHH', 0, 1, 96)
    data = b''
    if version:
        data += _event(199, version.encode('ascii') + b'\x00')
    if title:
        data += _event(194, title.encode('utf-16-le'))
    for s in samples:
        data += _event(196, s.encode('utf-16-le'))
    data += extra_events
    body = b'FLdt' + struct.pack('<I', len(data)) + data
    with open(path, 'wb') as f:
        f.write(header + body)
    return path


def make_wav(path, seconds, sr=44100, ch=2, bits=16, extra_chunks=b''):
    byte_rate = sr * ch * (bits // 8)
    data_size = int(round(byte_rate * seconds))
    fmt = struct.pack('<HHIIHH', 1, ch, sr, byte_rate, ch * (bits // 8), bits)
    fmt_chunk = b'fmt ' + struct.pack('<I', len(fmt)) + fmt
    data_chunk = b'data' + struct.pack('<I', data_size)  # declared, not written
    body = b'WAVE' + fmt_chunk + extra_chunks + data_chunk
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', len(body)) + body)
    return path


def acid_chunk(tempo=140.0, one_shot=False):
    flags = 0x01 if one_shot else 0x00
    payload = struct.pack('<IHHfIHHf', flags, 0, 0, 0.0, 0, 4, 4, tempo)
    return b'acid' + struct.pack('<I', len(payload)) + payload


def bext_chunk(originator='Pro Tools'):
    # BWF bext: Description[0:256], Originator[256:288], then more fields.
    body = b'\x00' * 256 + originator.encode('ascii').ljust(32, b'\x00')
    body = body.ljust(602, b'\x00')
    return b'bext' + struct.pack('<I', len(body)) + body


def _ext80(rate):
    import math
    if rate <= 0:
        return b'\x00' * 10
    m, e = math.frexp(rate)
    exp = e - 1 + 16383
    mant = int(m * 2 * (1 << 63))
    return struct.pack('>H', exp) + struct.pack('>Q', mant)


def make_aiff(path, seconds, sr=48000, ch=2, bits=16):
    nframes = int(round(sr * seconds))
    comm = struct.pack('>H', ch) + struct.pack('>I', nframes) + \
        struct.pack('>H', bits) + _ext80(sr)
    comm_chunk = b'COMM' + struct.pack('>I', len(comm)) + comm
    ssnd = b'SSND' + struct.pack('>I', 8) + b'\x00' * 8
    form_body = b'AIFF' + comm_chunk + ssnd
    with open(path, 'wb') as f:
        f.write(b'FORM' + struct.pack('>I', len(form_body)) + form_body)
    return path
