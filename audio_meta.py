"""Read duration and light metadata from audio files using ONLY the Python
standard library — no numpy, mutagen, ffmpeg or soundfile.

The point is a zero-dependency app: a producer double-clicks one file and it
just runs. Every probe is defensive and never raises; anything it can't make
sense of comes back as ``duration=None`` with the rest best-effort.

Supported containers: WAV/RF64, AIFF/AIFC, FLAC, Ogg (Vorbis/Opus) and MP3.
For each we read only the header region (and, for Ogg/MP3, a small tail), never
the whole audio payload, so probing thousands of files stays fast.

Alongside duration we surface any *encoder / software* tag, because a render
exported by FL Studio sometimes stamps one ("FL Studio ..."), which is a strong
hint the file is a render rather than a downloaded sample.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

AUDIO_EXTENSIONS = (
    '.wav', '.mp3', '.flac', '.ogg', '.oga', '.opus', '.aif', '.aiff', '.aifc',
)

_FL_MARKERS = ('fl studio', 'image-line', 'imageline', 'fruityloops')


@dataclass
class AudioInfo:
    path: str
    codec: str = ''             # 'wav','mp3','flac','ogg','opus','aiff'
    duration: float | None = None    # seconds
    sample_rate: int | None = None
    channels: int | None = None
    bit_depth: int | None = None
    bitrate: int | None = None       # bits/sec, for lossy
    encoder: str = ''           # software/encoder tag if present
    fl_signature: bool = False  # metadata explicitly names FL Studio / Image-Line
    # WAV chunk fingerprints — none is FL-exclusive, but together they hint at
    # provenance: FL writes an 'acid' tempo chunk into renders (historically on
    # by default), while a 'bext' chunk is written by Pro Tools / Reaper /
    # Nuendo and argues *against* an FL origin.
    has_acid: bool = False
    acid_tempo: float | None = None
    has_bext: bool = False
    has_markers: bool = False   # cue / smpl loop or slice markers present
    error: str = ''

    def note_encoder(self, text: str):
        text = (text or '').strip()
        if not text:
            return
        if not self.encoder:
            self.encoder = text
        if any(m in text.lower() for m in _FL_MARKERS):
            self.fl_signature = True


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _safe_text(raw: bytes, encoding='latin-1') -> str:
    try:
        return raw.decode(encoding, 'replace').strip('\x00').strip()
    except Exception:
        return ''


def _extended80_to_double(raw: bytes) -> float:
    """Decode an 80-bit IEEE-754 extended-precision float (AIFF sample rate)."""
    if len(raw) < 10:
        return 0.0
    exp = struct.unpack('>H', raw[:2])[0]
    mant = struct.unpack('>Q', raw[2:10])[0]
    sign = -1.0 if exp & 0x8000 else 1.0
    exp &= 0x7FFF
    if exp == 0 and mant == 0:
        return 0.0
    if exp == 0x7FFF:
        return 0.0  # inf / nan — treat as unknown
    # The 64-bit mantissa is explicit (includes the integer bit), unlike the
    # 53-bit implicit-bit mantissa of a normal double.
    return sign * mant * 2.0 ** (exp - 16383 - 63)


# --------------------------------------------------------------------------
# WAV / RF64
# --------------------------------------------------------------------------

def _probe_wav(f, info: AudioInfo):
    head = f.read(12)
    if len(head) < 12 or head[8:12] != b'WAVE':
        info.error = 'not a WAVE file'
        return
    is_rf64 = head[:4] in (b'RF64', b'BW64')
    info.codec = 'wav'
    byte_rate = 0
    data_size = None
    ds64_data_size = None

    for _ in range(64):  # hard cap on chunk walk
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        cid, size = hdr[:4], struct.unpack('<I', hdr[4:8])[0]
        if cid == b'ds64':
            body = f.read(min(size, 512))
            if len(body) >= 16:
                # riffSize(8), dataSize(8), sampleCount(8), ...
                ds64_data_size = struct.unpack('<Q', body[8:16])[0]
            _skip_pad(f, size, len(body))
            continue
        if cid == b'fmt ':
            body = f.read(min(size, 512))
            if len(body) >= 16:
                (audio_fmt, channels, sample_rate, br,
                 _block_align, bits) = struct.unpack('<HHIIHH', body[:16])
                info.channels = channels or None
                info.sample_rate = sample_rate or None
                info.bit_depth = bits or None
                byte_rate = br
            _skip_pad(f, size, len(body))
            continue
        if cid == b'data':
            data_size = size
            if is_rf64 and (size == 0xFFFFFFFF) and ds64_data_size is not None:
                data_size = ds64_data_size
            # Don't read the audio; we have what we need. Stop early unless we
            # still want trailing metadata — most files put data last anyway.
            if info.encoder:
                break
            # Seek past data to look for a trailing LIST/INFO or id3 chunk.
            try:
                f.seek(data_size + (data_size & 1), os.SEEK_CUR)
            except (OSError, ValueError):
                break
            continue
        if cid in (b'LIST', b'bext', b'ID3 ', b'id3 '):
            body = f.read(min(size, 4096))
            if cid == b'bext':
                info.has_bext = True
            _scan_riff_meta(cid, body, info)
            _skip_pad(f, size, len(body))
            continue
        if cid == b'acid':
            info.has_acid = True
            body = f.read(min(size, 24))
            if len(body) >= 24:
                tempo = struct.unpack_from('<f', body, 20)[0]
                if 20.0 < tempo < 400.0:
                    info.acid_tempo = round(tempo, 3)
            _skip_pad(f, size, len(body))
            continue
        if cid in (b'cue ', b'smpl'):
            info.has_markers = True
            try:
                f.seek(size + (size & 1), os.SEEK_CUR)
            except (OSError, ValueError):
                break
            continue
        # Unknown chunk: skip its (word-aligned) payload.
        try:
            f.seek(size + (size & 1), os.SEEK_CUR)
        except (OSError, ValueError):
            break

    # A 0xFFFFFFFF data size is the "unknown / streaming" sentinel — computing a
    # duration from it would be nonsense (unless RF64/ds64 already replaced it).
    if data_size == 0xFFFFFFFF:
        data_size = None
    if byte_rate and data_size:
        info.duration = data_size / byte_rate
    elif (info.sample_rate and info.channels and info.bit_depth and data_size):
        frame = info.channels * (info.bit_depth // 8)
        if frame:
            info.duration = data_size / (frame * info.sample_rate)


def _skip_pad(f, size, already_read):
    """Seek over the remainder of a chunk, honoring RIFF word alignment."""
    remainder = size - already_read + (size & 1)
    if remainder > 0:
        try:
            f.seek(remainder, os.SEEK_CUR)
        except (OSError, ValueError):
            pass


def _scan_riff_meta(cid: bytes, body: bytes, info: AudioInfo):
    if cid == b'LIST' and body[:4] == b'INFO':
        # Walk INFO sub-chunks for ISFT (software).
        pos = 4
        while pos + 8 <= len(body):
            sid = body[pos:pos + 4]
            slen = struct.unpack_from('<I', body, pos + 4)[0]
            val = body[pos + 8:pos + 8 + slen]
            if sid in (b'ISFT', b'ITCH', b'IENG'):
                info.note_encoder(_safe_text(val))
            pos += 8 + slen + (slen & 1)
    elif cid == b'bext':
        # Broadcast-Wave layout: Description[0:256], then Originator[256:288].
        # Software/DAW name often lands in Originator.
        info.note_encoder(_safe_text(body[256:256 + 32]))  # Originator
    elif cid in (b'ID3 ', b'id3 '):
        _scan_id3v2(body, info)


# --------------------------------------------------------------------------
# AIFF / AIFC
# --------------------------------------------------------------------------

def _probe_aiff(f, info: AudioInfo):
    head = f.read(12)
    if len(head) < 12 or head[8:12] not in (b'AIFF', b'AIFC'):
        info.error = 'not an AIFF file'
        return
    info.codec = 'aiff'
    num_frames = None
    sample_rate = 0.0
    for _ in range(64):
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        cid, size = hdr[:4], struct.unpack('>I', hdr[4:8])[0]
        if cid == b'COMM':
            body = f.read(min(size, 128))
            if len(body) >= 18:
                channels = struct.unpack('>H', body[0:2])[0]
                num_frames = struct.unpack('>I', body[2:6])[0]
                bits = struct.unpack('>H', body[6:8])[0]
                sample_rate = _extended80_to_double(body[8:18])
                info.channels = channels or None
                info.bit_depth = bits or None
                info.sample_rate = int(sample_rate) or None
            _skip_pad(f, size, len(body))
            continue
        if cid in (b'ANNO', b'NAME', b'AUTH', b'(c) ', b'APPL'):
            body = f.read(min(size, 2048))
            info.note_encoder(_safe_text(body))
            _skip_pad(f, size, len(body))
            continue
        try:
            f.seek(size + (size & 1), os.SEEK_CUR)
        except (OSError, ValueError):
            break
    if num_frames and sample_rate > 0:
        info.duration = num_frames / sample_rate


# --------------------------------------------------------------------------
# FLAC
# --------------------------------------------------------------------------

def _probe_flac(f, info: AudioInfo):
    if f.read(4) != b'fLaC':
        info.error = 'not a FLAC file'
        return
    info.codec = 'flac'
    for _ in range(64):
        hdr = f.read(4)
        if len(hdr) < 4:
            break
        last = bool(hdr[0] & 0x80)
        btype = hdr[0] & 0x7F
        length = struct.unpack('>I', b'\x00' + hdr[1:4])[0]
        body = f.read(length)
        if btype == 0 and len(body) >= 18:  # STREAMINFO
            # bytes 10..17 pack: sampleRate(20) channels(3) bits(5) totalSamples(36)
            bits64 = int.from_bytes(body[10:18], 'big')
            sample_rate = bits64 >> 44
            channels = ((bits64 >> 41) & 0x7) + 1
            bit_depth = ((bits64 >> 36) & 0x1F) + 1
            total_samples = bits64 & ((1 << 36) - 1)
            info.sample_rate = sample_rate or None
            info.channels = channels
            info.bit_depth = bit_depth
            if sample_rate and total_samples:
                info.duration = total_samples / sample_rate
        elif btype == 4:  # VORBIS_COMMENT
            _scan_vorbis_comment(body, info)
        if last:
            break


def _scan_vorbis_comment(body: bytes, info: AudioInfo):
    try:
        pos = 0
        vlen = struct.unpack_from('<I', body, pos)[0]
        vendor = _safe_text(body[4:4 + vlen], 'utf-8')
        pos += 4 + vlen  # skip vendor string
        count = struct.unpack_from('<I', body, pos)[0]
        pos += 4
        # Named ENCODER/SOFTWARE comments beat the generic vendor string, so
        # scan them first and fall back to the vendor only if nothing was set.
        for _ in range(min(count, 256)):
            clen = struct.unpack_from('<I', body, pos)[0]
            pos += 4
            entry = body[pos:pos + clen].decode('utf-8', 'replace')
            pos += clen
            if '=' in entry:
                key, val = entry.split('=', 1)
                if key.upper() in ('ENCODER', 'ENCODED_BY', 'SOFTWARE'):
                    info.note_encoder(val)
        info.note_encoder(vendor)
    except (struct.error, IndexError):
        pass


# --------------------------------------------------------------------------
# Ogg (Vorbis / Opus)
# --------------------------------------------------------------------------

def _probe_ogg(f, info: AudioInfo):
    head = f.read(65536)
    if head[:4] != b'OggS':
        info.error = 'not an Ogg file'
        return
    info.codec = 'ogg'
    # The first page's packet carries the identification header.
    idx = head.find(b'\x01vorbis')
    if idx != -1 and idx + 16 <= len(head):
        info.codec = 'ogg'
        info.channels = head[idx + 11] or None
        info.sample_rate = struct.unpack_from('<I', head, idx + 12)[0] or None
    else:
        idx = head.find(b'OpusHead')
        if idx != -1 and idx + 19 <= len(head):
            info.codec = 'opus'
            info.channels = head[idx + 9] or None
            pre_skip = struct.unpack_from('<H', head, idx + 10)[0]
            info.sample_rate = struct.unpack_from('<I', head, idx + 12)[0] or None
            info._opus_preskip = pre_skip  # type: ignore[attr-defined]
    # Encoder is in the comment header (second packet); a cheap scan finds it.
    enc = head.find(b'ENCODER=')
    if enc != -1:
        end = head.find(b'\x00', enc)
        chunk = head[enc + 8: end if 0 <= end - enc - 8 < 256 else enc + 8 + 128]
        info.note_encoder(_safe_text(chunk.split(b'\x00', 1)[0], 'utf-8'))

    granule = _last_ogg_granule(f)
    if granule is not None and granule >= 0:
        if info.codec == 'opus':
            pre = getattr(info, '_opus_preskip', 0)
            info.duration = max(granule - pre, 0) / 48000.0
        elif info.sample_rate:
            info.duration = granule / info.sample_rate


def _last_ogg_granule(f):
    """Granule position of the final Ogg page = total decoded samples."""
    try:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        window = min(end, 65536)
        f.seek(end - window)
        tail = f.read(window)
    except (OSError, ValueError):
        return None
    pos = tail.rfind(b'OggS')
    if pos == -1 or pos + 14 > len(tail):
        return None
    try:
        return struct.unpack_from('<q', tail, pos + 6)[0]
    except struct.error:
        return None


# --------------------------------------------------------------------------
# MP3
# --------------------------------------------------------------------------

_MP3_BITRATES = {
    # (version_bit, layer): [by bitrate index]
    (3, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (3, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_BITRATES[(2, 3)] = _MP3_BITRATES[(2, 2)]  # v2/v2.5 layer3 shares layer2 table
_MP3_SRATES = {
    3: [44100, 48000, 32000],   # MPEG1
    2: [22050, 24000, 16000],   # MPEG2
    0: [11025, 12000, 8000],    # MPEG2.5
}
_MP3_SPF = {  # samples per frame: [layer1, layer2, layer3]
    3: {1: 384, 2: 1152, 3: 1152},
    2: {1: 384, 2: 1152, 3: 576},
    0: {1: 384, 2: 1152, 3: 576},
}


def _probe_mp3(f, info: AudioInfo, file_size: int):
    info.codec = 'mp3'
    data = f.read(10)
    id3_size = 0
    if data[:3] == b'ID3':
        # Syncsafe tag size from the 10-byte header we already have.
        id3_size = 10 + _syncsafe(data[6:10])
        # Pull the ID3v2 tag body to mine TSSE/TENC (encoder) frames.
        tag_body = f.read(min(id3_size - 10, 1 << 20))
        _scan_id3v2_frames(data, tag_body, info)
        f.seek(id3_size)
    else:
        f.seek(0)

    # Find the first frame sync within a reasonable window.
    buf = f.read(1 << 16)
    off = _find_mp3_frame(buf)
    if off is None:
        info.error = 'no MP3 frame sync found'
        return
    hdr = buf[off:off + 4]
    parsed = _parse_mp3_header(hdr)
    if not parsed:
        info.error = 'bad MP3 frame header'
        return
    ver, layer, bitrate, srate, padding, channels = parsed
    info.sample_rate = srate or None
    info.channels = channels
    info.bitrate = bitrate * 1000 if bitrate else None
    spf = _MP3_SPF[ver][layer]

    # VBR headers (Xing/Info/VBRI) give an exact frame count.
    frame = buf[off:off + 200]
    n_frames = _xing_frames(frame)
    if n_frames and srate:
        info.duration = n_frames * spf / srate
        return
    # CBR estimate over the audio bytes (file minus tags). Check the real file
    # tail for an ID3v1 'TAG' — not buf, which is only the 64 KB sync window.
    audio_bytes = file_size - id3_size
    if _has_id3v1(f, file_size):
        audio_bytes -= 128
    if bitrate:
        info.duration = audio_bytes / (bitrate * 1000 / 8)


def _syncsafe(b: bytes) -> int:
    if len(b) < 4:
        return 0
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _has_id3v1(f, file_size) -> bool:
    try:
        f.seek(max(file_size - 128, 0))
        return f.read(3) == b'TAG'
    except (OSError, ValueError):
        return False


def _find_mp3_frame(buf: bytes):
    i = 0
    n = len(buf)
    while i < n - 1:
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
            if _parse_mp3_header(buf[i:i + 4]):
                return i
        i += 1
    return None


def _parse_mp3_header(hdr: bytes):
    if len(hdr) < 4 or hdr[0] != 0xFF or (hdr[1] & 0xE0) != 0xE0:
        return None
    ver_bits = (hdr[1] >> 3) & 0x3   # 3=MPEG1, 2=MPEG2, 0=MPEG2.5, 1=reserved
    layer_bits = (hdr[1] >> 1) & 0x3  # 3=LayerI,2=II,1=III,0=reserved
    if ver_bits == 1 or layer_bits == 0:
        return None
    ver = ver_bits
    layer = {3: 1, 2: 2, 1: 3}[layer_bits]
    br_index = (hdr[2] >> 4) & 0xF
    sr_index = (hdr[2] >> 2) & 0x3
    padding = (hdr[2] >> 1) & 0x1
    chan_mode = (hdr[3] >> 6) & 0x3
    if br_index in (0, 15) or sr_index == 3:
        return None
    table = _MP3_BITRATES.get((ver, layer))
    if not table:
        return None
    bitrate = table[br_index]
    srate = _MP3_SRATES[ver][sr_index]
    channels = 1 if chan_mode == 3 else 2
    return ver, layer, bitrate, srate, padding, channels


def _xing_frames(frame: bytes):
    for tag in (b'Xing', b'Info'):
        p = frame.find(tag)
        if p != -1 and p + 8 <= len(frame):
            flags = struct.unpack('>I', frame[p + 4:p + 8])[0]
            if flags & 0x1 and p + 12 <= len(frame):
                return struct.unpack('>I', frame[p + 8:p + 12])[0]
    p = frame.find(b'VBRI')
    if p != -1 and p + 18 <= len(frame):
        return struct.unpack('>I', frame[p + 14:p + 18])[0]
    return None


# --------------------------------------------------------------------------
# ID3v2 text frames (encoder tags)
# --------------------------------------------------------------------------

def _scan_id3v2(body: bytes, info: AudioInfo):
    if body[:3] != b'ID3':
        return
    _scan_id3v2_frames(body[:10], body[10:], info)


def _decode_id3_text(raw: bytes) -> str:
    if not raw:
        return ''
    enc = raw[0]
    payload = raw[1:]
    try:
        if enc == 0:
            return payload.decode('latin-1', 'replace').strip('\x00').strip()
        if enc == 1:
            return payload.decode('utf-16', 'replace').strip('\x00').strip()
        if enc == 2:
            return payload.decode('utf-16-be', 'replace').strip('\x00').strip()
        return payload.decode('utf-8', 'replace').strip('\x00').strip()
    except Exception:
        return ''


def _scan_id3v2_frames(header10: bytes, body: bytes, info: AudioInfo):
    if len(header10) < 10:
        return
    major = header10[3]
    pos = 0
    wanted = {b'TSSE', b'TENC', b'TSS', b'TSS ', b'COMM'}
    for _ in range(256):
        if pos + 10 > len(body):
            break
        fid = body[pos:pos + 4]
        if fid == b'\x00\x00\x00\x00' or not fid.strip(b'\x00'):
            break
        if major == 2:  # ID3v2.2 uses 3-byte ids + 3-byte sizes
            fid = body[pos:pos + 3]
            size = int.from_bytes(body[pos + 3:pos + 6], 'big')
            fpos = pos + 6
            pos = fpos + size
            if fid in (b'TSS', b'TEN'):
                info.note_encoder(_decode_id3_text(body[fpos:fpos + size]))
            continue
        size = (struct.unpack('>I', body[pos + 4:pos + 8])[0] if major == 3
                else _syncsafe(body[pos + 4:pos + 8]))
        fpos = pos + 10
        frame = body[fpos:fpos + size]
        if fid in (b'TSSE', b'TENC'):
            info.note_encoder(_decode_id3_text(frame))
        pos = fpos + size


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

_SNIFFERS = [
    (b'RIFF', _probe_wav), (b'RF64', _probe_wav), (b'BW64', _probe_wav),
    (b'FORM', _probe_aiff),
    (b'fLaC', _probe_flac),
    (b'OggS', _probe_ogg),
]


def probe(path) -> AudioInfo:
    """Probe one audio file. Never raises; failures land in ``.error``."""
    info = AudioInfo(path=str(path))
    try:
        file_size = os.path.getsize(path)
        with open(path, 'rb') as f:
            magic = f.read(4)
            f.seek(0)
            fn = None
            for sig, handler in _SNIFFERS:
                if magic == sig:
                    fn = handler
                    break
            if fn is _probe_wav and magic == b'RIFF':
                # confirm it's WAVE (could be AVI/ANI); handler re-checks anyway
                pass
            if fn is not None:
                fn(f, info)
            elif magic[:3] == b'ID3' or (len(magic) >= 2 and magic[0] == 0xFF
                                         and (magic[1] & 0xE0) == 0xE0):
                _probe_mp3(f, info, file_size)
            else:
                # Fall back on the extension for headerless guesses.
                ext = os.path.splitext(path)[1].lower()
                if ext == '.mp3':
                    _probe_mp3(f, info, file_size)
                else:
                    info.error = 'unrecognized audio container'
    except Exception as exc:
        # The public contract is that probe() never raises, so anything odd
        # (truncated headers, malformed chunks, etc.) becomes an error string.
        info.error = f'{exc.__class__.__name__}'
    return info


if __name__ == '__main__':
    import sys

    for arg in sys.argv[1:]:
        a = probe(arg)
        dur = f'{a.duration:.2f}s' if a.duration is not None else '?'
        print(f'{os.path.basename(arg)}')
        print(f'   codec={a.codec or "?"} dur={dur} sr={a.sample_rate} '
              f'ch={a.channels} bits={a.bit_depth} br={a.bitrate}')
        if a.encoder:
            print(f'   encoder={a.encoder!r}  fl={a.fl_signature}')
        if a.error:
            print(f'   error={a.error}')
