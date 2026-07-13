"""Parser for FL Studio project (.flp) files.

Extracts the sample file paths referenced by a project, plus the project
title and the FL Studio version that saved it. Works on the raw binary
event stream with no third-party dependencies.

This module is shared, byte-for-byte, with the sister project
FLSearchBySample so that any fix to the FLP format handling benefits both.

FLP format (verified against real FL Studio 20.1 and 25.2 projects):

    "FLhd" <u32 header_len> <header bytes>
    "FLdt" <u32 data_len>   <event stream>

Event stream: a 1-byte event id determines the payload size:

    id   0..63   -> 1 byte
    id  64..127  -> 2 bytes
    id 128..191  -> 4 bytes
    id 192..255  -> varint length (7 bits per byte, high bit = continue),
                    then that many payload bytes

Text payloads are UTF-16LE in FL Studio >= 11.5 and ANSI before that.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

EVENT_TITLE = 194
EVENT_SAMPLE_PATH = 196
EVENT_VERSION = 199

# Extensions used when scanning opaque plugin data blobs (FPC, DirectWave,
# Slicex, ...) for sample references they hold outside of event 196.
_BLOB_EXTENSIONS = (
    '.wav', '.mp3', '.flac', '.ogg', '.aif', '.aiff', '.wv', '.m4a', '.ds',
)

# A run of at least 5 printable-ASCII UTF-16LE characters.
_UTF16_RUN = re.compile(rb'(?:[ -~]\x00){5,}')


@dataclass
class ProjectInfo:
    path: str
    version: str = ''
    title: str = ''
    samples: list = field(default_factory=list)          # event 196 paths
    plugin_samples: list = field(default_factory=list)   # best effort, from plugin blobs
    error: str = ''


def _decode_text(payload: bytes) -> str:
    """Decode an FLP text payload, auto-detecting UTF-16LE vs ANSI."""
    if len(payload) >= 2 and len(payload) % 2 == 0:
        high_bytes = payload[1::2]
        if high_bytes.count(0) >= len(high_bytes) * 0.7:
            try:
                return payload.decode('utf-16-le', 'replace').rstrip('\x00')
            except Exception:
                pass
    return payload.decode('latin-1', 'replace').rstrip('\x00')


def _raw_sample_sweep(data: bytes):
    """Best-effort recall net: every UTF-16LE string in the file that ends in
    an audio extension. Catches samples inside opaque plugin blobs (FPC,
    DirectWave, Slicex, ...) and survives any event-stream misalignment."""
    for match in _UTF16_RUN.finditer(data):
        text = match.group(0).decode('utf-16-le', 'ignore').rstrip('\x00')
        if text.lower().endswith(_BLOB_EXTENSIONS):
            yield text


def _version_tuple(version: str):
    parts = []
    for chunk in version.split('.')[:3]:
        if not chunk.isdigit():
            return ()
        parts.append(int(chunk))
    return tuple(parts)


def parse_flp(path) -> ProjectInfo:
    """Parse one .flp file. Never raises: failures land in ProjectInfo.error."""
    info = ProjectInfo(path=str(path))
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as exc:
        info.error = f'unreadable ({exc.__class__.__name__})'
        return info

    if len(data) < 12 or data[:4] != b'FLhd':
        info.error = 'not an FLP file'
        return info

    header_len = struct.unpack_from('<I', data, 4)[0]
    pos = 8 + header_len
    if data[pos:pos + 8][:4] != b'FLdt' or pos + 8 > len(data):
        info.error = 'missing FLdt chunk'
        return info
    data_len = struct.unpack_from('<I', data, pos + 4)[0]
    pos += 8
    end = min(pos + data_len, len(data))

    samples = {}          # dict used as an ordered set
    plugin_samples = {}
    truncated = False
    ver = ()

    while pos < end:
        event_id = data[pos]
        pos += 1
        if event_id < 64:
            pos += 1
        elif event_id < 128:
            pos += 2
        elif event_id < 192:
            # FL Studio 25.2.4+ breaks its own TLV rule: header event 172
            # carries 3 bytes, not 4 (verified byte-level against real
            # projects saved by 25.2.0/25.2.4/25.2.5/26.1).
            if event_id == 172 and ver >= (25, 2, 4):
                pos += 3
            else:
                pos += 4
        else:
            length = 0
            shift = 0
            while True:
                if pos >= end:
                    truncated = True
                    break
                byte = data[pos]
                pos += 1
                length |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            if truncated:
                break
            payload = data[pos:pos + length]
            pos += length
            if len(payload) < length:
                truncated = True  # keep whatever the partial payload yields

            if event_id == EVENT_SAMPLE_PATH:
                text = _decode_text(payload)
                if text:
                    samples[text] = None
            elif event_id == EVENT_VERSION and not info.version:
                info.version = payload.rstrip(b'\x00').decode('ascii', 'replace')
                ver = _version_tuple(info.version)
            elif event_id == EVENT_TITLE and not info.title:
                info.title = _decode_text(payload)

    if truncated:
        info.error = 'unexpected structure (partial results)'

    known = {s.lower() for s in samples}
    known.update(s.replace('/', '\\').rsplit('\\', 1)[-1].lower() for s in samples)
    for text in _raw_sample_sweep(data[8 + header_len + 8:end]):
        low = text.lower()
        if low not in known:
            known.add(low)
            plugin_samples[text] = None

    info.samples = list(samples)
    info.plugin_samples = list(plugin_samples)
    return info


if __name__ == '__main__':
    import sys

    for arg in sys.argv[1:]:
        result = parse_flp(arg)
        print(f'=== {result.path}')
        print(f'    version: {result.version or "?"}   title: {result.title or "-"}'
              f'   error: {result.error or "-"}')
        for s in result.samples:
            print(f'    sample: {s}')
        for s in result.plugin_samples:
            print(f'    plugin: {s}')
