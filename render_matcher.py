"""Decide which rendered audio files belong to which FL Studio project.

This is the hard part of FLIndexRenders. A producer's drive holds thousands of
audio files that are mostly *samples*, and buried among them are the handful
that are actually *renders* (bounces/exports) of their projects. There is no
reliable "made by FL Studio" fingerprint in an audio file, so matching leans on
structure:

  * FL's Export dialog pre-fills the output filename with the project name, so
    an exact (normalized) name match is the backbone signal.
  * FL exports stems into a folder named after the project, so a folder whose
    name matches a project attributes everything inside it to that project.
  * A file that a project uses as an *input* sample cannot be that project's
    *output* render — a hard, per-project exclusion.
  * Duration/location/tempo-chunk are corroborating signals, never enough on
    their own to invent a match.

Everything here is pure string/number logic over lightweight dict records, so
it is deterministic and unit-testable without touching the disk.

An audio record is a dict built by ``make_audio_record``. A project record is
built by ``make_project_record`` (pass the project's sample paths so the
per-project exclusion works).
"""

from __future__ import annotations

import os
import re
import unicodedata

# Folder names that imply "this is where renders live".
RENDER_DIR_NAMES = {
    'rendered', 'renders', 'render', 'export', 'exports', 'exported',
    'bounce', 'bounces', 'bounced', 'mixdown', 'mixdowns', 'mixes',
    'out', 'output', 'outputs', 'master', 'masters', 'mastered',
    'stems', 'stem', 'released', 'release', 'final', 'finals', 'mp3', 'mp3s',
}

# Audio containers we treat as plausible renders.
RENDER_EXTS = {
    '.wav', '.mp3', '.flac', '.ogg', '.oga', '.opus', '.aif', '.aiff', '.aifc',
}

# Trailing/leading "decoration" tokens stripped when trying to recover the
# project name from a render name (e.g. "My Beat - master 2" -> "my beat").
# Deliberately conservative: words that commonly form part of a real track
# title (beat, flip, loop, new, out, edit, ...) are NOT here — over-stripping
# them would collapse good names into generic stubs. The strict tier is the
# safety net for any name that legitimately contains one of these words.
_DECOR_TOKENS = {
    'master', 'mastered', 'mastering', 'mixdown', 'mixed', 'mix', 'final',
    'finalmix', 'wip', 'draft', 'rough', 'demo', 'bounce', 'bounced',
    'render', 'rendered', 'export', 'exported', 'snippet',
    'remaster', 'remastered', 'instrumental', 'inst', 'acapella',
    'radioedit', 'copy', 'ver', 'version', 'take', 'normalized',
    'wav', 'mp3', 'flac', 'ogg', 'bpm', 'lufs',
}

# Project names too generic to match on name alone — these need an exact name
# hit *and* folder proximity before we'll tie a render to them.
_GENERIC_STEMS = {
    'untitled', 'untitled project', 'new', 'new song', 'song', 'project',
    'audio', 'track', 'beat', 'loop', 'idea', 'sketch', 'test', 'demo',
    'export', 'render', 'rendered', 'mixdown', 'mix', 'master', 'final',
    'temp', 'default', 'flip', 'wip', 'bounce', 'out', 'output', 'stems',
}

_SRATE_TOKENS = {'44100', '48000', '96000', '88200', '192000', '22050',
                 '44k', '48k', '96k', '441'}

_COPY_SUFFIX = re.compile(r'\s*[-–—]?\s*copy(\s*\(?\d+\)?)?$', re.I)
_PAREN_NUM = re.compile(r'\s*[\(\[]\s*\d{1,3}\s*[\)\]]\s*$')   # trailing "(2)"/"[3]"
_VER_RE = re.compile(r'^v\d+(\.\d+)?$')                        # v2, v3.1
_TAKE_RE = re.compile(r'^(take|takes|pt|part|ver|version)\d*$')
_BPM_RE = re.compile(r'^\d{2,3}bpm$')
_BITDEPTH_RE = re.compile(r'^\d{1,2}bit$')
_KHZ_RE = re.compile(r'^\d{2,3}k$')
_KEY_RE = re.compile(r'^[a-g](?:#|b|s)?(?:maj|min|m)$|^[a-g](?:#|b|s)(?:maj|min|m)?$')
_SEP_RUN = re.compile(r'[\s_\-.]+')


def _fold(s: str) -> str:
    """Unicode-fold: NFKC, drop symbols/emoji/control chars, casefold."""
    s = unicodedata.normalize('NFKC', s)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        out.append(' ' if cat[0] in ('S', 'C') else ch)
    return unicodedata.normalize('NFKC', ''.join(out)).casefold()


def _ascii_fold(s: str) -> str:
    """Strip combining accents so 'café' and 'cafe' can meet at a lower tier."""
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))


def normalize_strict(stem: str) -> str:
    """Light normalization: unicode-fold, separators, copy/paren markers. This
    is the identity a render shares with its project when FL auto-named it."""
    s = _fold(stem)
    s = _PAREN_NUM.sub('', s)
    s = _COPY_SUFFIX.sub('', s)
    s = re.sub(r'[\(\)\[\]\{\}]', ' ', s)
    s = _SEP_RUN.sub(' ', s).strip()
    return s


def _is_num_decor(token: str) -> bool:
    """Purely numeric decorations, safe to strip from the *front* of a name:
    track numbers, "v2", bpm, bit depth, sample rate. Deliberately excludes
    bare keyword words like 'version'/'take'/'part' — leading those are usually
    real title words ('Version Control', 'Take Five')."""
    if not token:
        return True
    if token in _SRATE_TOKENS:
        return True
    if (_VER_RE.match(token) or _BPM_RE.match(token)
            or _BITDEPTH_RE.match(token) or _KHZ_RE.match(token)):
        return True
    return token.isdigit() and len(token) <= 4  # track#, year, bpm


def _is_decor(token: str) -> bool:
    """A token safe to strip from the *end* of a name — includes keyword
    decorations (master/mixdown/...), take/part/version words, musical keys,
    plus all numeric ones."""
    return (token in _DECOR_TOKENS or _is_num_decor(token)
            or bool(_TAKE_RE.match(token)) or bool(_KEY_RE.match(token)))


def normalize_loose(stem: str) -> str:
    """Aggressive normalization: strip leading/trailing render decorations so
    "01 my beat (master) 140 Fmin" collapses toward the project name "my beat".
    Only ever used as a *secondary* match tier — the strict form protects names
    that legitimately contain a decoration word (e.g. a track titled 'Final')."""
    s = normalize_strict(stem)
    tokens = s.split(' ')
    # Trailing tokens: strip keyword + numeric decorations ("... master 140").
    while len(tokens) > 1 and _is_decor(tokens[-1]):
        tokens.pop()
    # Leading tokens: only strip numeric decorations (track numbers, "v2"),
    # never keywords — "Final Boss"/"Master Plan" must keep their first word.
    while len(tokens) > 1 and _is_num_decor(tokens[0]):
        tokens.pop(0)
    return ' '.join(tokens).strip()


def is_generic(strict: str, loose: str) -> bool:
    return (not loose or loose in _GENERIC_STEMS or strict in _GENERIC_STEMS
            or len(loose) < 4 or loose.isdigit())


def _folder_keys(path: str) -> set:
    """Normalized names of the immediate parent and grandparent folders, used to
    attribute stem/export folders ('.../My Beat/Kick.wav') to a project."""
    keys = set()
    d = os.path.dirname(path)
    for _ in range(2):
        if not d:
            break
        name = os.path.basename(os.path.normpath(d))
        if name and name.lower() not in RENDER_DIR_NAMES:
            ks, kl = normalize_strict(name), normalize_loose(name)
            if len(ks) >= 4:
                keys.add(ks)
            if len(kl) >= 4:
                keys.add(kl)
        d = os.path.dirname(d)
    return keys


def make_audio_record(path, duration=None, used_as_sample=False,
                      fl_signature=False, has_acid=False, has_bext=False,
                      mtime=0, **extra):
    stem, ext = os.path.splitext(os.path.basename(path))
    strict = normalize_strict(stem)
    rec = {
        'path': path,
        'stem': stem,
        'ext': ext.lower(),
        'dir': os.path.dirname(path),
        'duration': duration,
        'used_as_sample': used_as_sample,
        'fl_signature': fl_signature,
        'has_acid': has_acid,
        'has_bext': has_bext,
        'mtime': mtime,
        'key_strict': strict,
        'key_loose': normalize_loose(stem),
        'key_ascii': _ascii_fold(strict),
        'folder_keys': _folder_keys(path),
        'basename_lower': os.path.basename(path).lower(),
    }
    rec.update(extra)
    return rec


def make_project_record(path, samples=(), mtime=0, **extra):
    stem = os.path.splitext(os.path.basename(path))[0]
    strict = normalize_strict(stem)
    loose = normalize_loose(stem)
    samples_lower = set()
    for s in samples:
        base = s.replace('/', '\\').rsplit('\\', 1)[-1]
        if base:
            samples_lower.add(base.lower())
    rec = {
        'path': path,
        'stem': stem,
        'dir': os.path.dirname(path),
        'mtime': mtime,
        'key_strict': strict,
        'key_loose': loose,
        'key_ascii': _ascii_fold(strict),
        'generic': is_generic(strict, loose),
        'samples_lower': samples_lower,
    }
    rec.update(extra)
    return rec


def _dir_relation(audio_dir: str, project_dir: str) -> str:
    ad = os.path.normcase(os.path.normpath(audio_dir))
    pd = os.path.normcase(os.path.normpath(project_dir))
    base = os.path.basename(ad)
    render_ish = base in RENDER_DIR_NAMES
    sep = os.sep
    if ad == pd:
        return 'same'
    if render_ish and os.path.dirname(ad) == pd:
        return 'render_child'          # <project>/Rendered/track.wav
    if render_ish and os.path.dirname(ad) == os.path.dirname(pd):
        return 'render_sibling'        # <parent>/{Project, Rendered}/...
    if ad.startswith(pd + sep) or pd.startswith(ad + sep):
        return 'nested'
    if render_ish:
        return 'render_ish'
    return 'far'


_REL_POINTS = {
    'same': 40, 'render_child': 38, 'render_sibling': 30,
    'nested': 22, 'render_ish': 12, 'far': 0,
}
_REL_LABEL = {
    'same': 'same folder', 'render_child': 'in render subfolder',
    'render_sibling': 'in render folder', 'nested': 'nearby folder',
    'render_ish': 'render-named folder', 'far': '',
}
_NEAR = ('same', 'render_child', 'render_sibling', 'nested')


def score_pair(audio: dict, project: dict) -> tuple[int, str]:
    """Score how likely `audio` is a render of `project`. 0 == not a match."""
    # Hard exclusion: a file this project uses as an input sample is not this
    # project's output render. (Evaluated per project, so a render re-imported
    # into a *different* project still matches its own project.)
    if audio['basename_lower'] in project['samples_lower']:
        return 0, ''

    a_strict, p_strict = audio['key_strict'], project['key_strict']
    a_loose, p_loose = audio['key_loose'], project['key_loose']
    rel = _dir_relation(audio['dir'], project['dir'])
    folder_hit = (not project['generic'] and p_strict
                  and (p_strict in audio['folder_keys']
                       or (p_loose and p_loose in audio['folder_keys'])))

    if project['generic']:
        # Generic names ('Untitled', 'beat') need exact name AND to sit by the
        # .flp, or they glom onto everything.
        if a_strict == p_strict and rel in _NEAR:
            base, kind = 82, 'exact name + same folder'
        else:
            return 0, ''
    elif a_strict == p_strict:
        base, kind = 100, 'exact name'
    elif folder_hit:
        base = 90 if rel in _NEAR else 62
        kind = 'in a folder named after the project'
    elif a_loose and a_loose == p_loose:
        base, kind = 72, 'name matches (minus master/mix/bpm tags)'
    elif (audio['key_ascii'] and audio['key_ascii'] == project['key_ascii']
          and (audio['key_ascii'] != a_strict or project['key_ascii'] != p_strict)):
        # Accents differ but the accent-folded names are identical ('café'/'cafe').
        base, kind = 66, 'name matches (accents folded)'
    elif _prefix_match(a_strict, p_strict):
        base, kind = 46, 'render name starts with project name'
    else:
        return 0, ''

    score = base + _REL_POINTS[rel]
    reasons = [kind]
    if rel != 'far' and 'folder' not in kind:
        reasons.append(_REL_LABEL[rel])

    dur = audio.get('duration')
    if dur is not None:
        if dur >= 60:
            score += 14
        elif dur >= 20:
            score += 8
        elif dur < 2:
            score -= 30
        elif dur < 5:
            score -= 16

    if audio.get('fl_signature'):
        score += 20
        reasons.append('FL Studio metadata')
    elif audio.get('has_acid'):
        score += 8
        reasons.append('tempo metadata')
    if audio.get('has_bext'):
        score -= 10           # bext => another DAW; weak evidence against FL

    return max(score, 0), ' · '.join(r for r in reasons if r)


def _prefix_match(a_strict: str, p_strict: str) -> bool:
    """Render stem begins with the whole project name at a word boundary.
    Guards against short common roots ('beat', 'trap') matching everything."""
    if len(p_strict) < 6:
        return False
    # The trailing space both enforces a word boundary and guarantees the
    # render name is strictly longer than the project name.
    if not a_strict.startswith(p_strict + ' '):
        return False
    # The project name must be a substantial fraction of the render name.
    return len(p_strict) >= 0.5 * len(a_strict)


# Score at/above which we consider an audio file a render of a project.
MATCH_THRESHOLD = 58


def confidence_of(score: int) -> str:
    if score >= 108:
        return 'high'
    if score >= 78:
        return 'medium'
    return 'low'


def variant_key(rec: dict):
    """Group identifier for the same render exported in several formats/bit
    depths (mybeat.wav + mybeat.mp3 + mybeat_24bit.wav = one render)."""
    dur = rec.get('duration')
    bucket = round(dur, 0) if isinstance(dur, (int, float)) else None
    return (rec.get('key_loose') or rec.get('key_strict'), bucket)


def count_unique_renders(records) -> int:
    return len({variant_key(r) for r in records}) if records else 0


def classify(projects, audios, orphan_min_duration: float = 45.0,
             include_long_unmatched: bool = False):
    """Assign each audio to its best-matching project.

    Returns (matches_by_project, orphans):
      matches_by_project: {project_path: [audio_rec + confidence/score/reason]}
      orphans:            [audio_rec + reason] that look like renders but map
                          to no known project.
    Audio matching no project and lacking render evidence is dropped, so the
    orphan bucket doesn't fill with the user's ordinary (unused) samples.
    """
    by_strict = {}
    by_loose = {}
    by_ascii = {}
    for p in projects:
        by_strict.setdefault(p['key_strict'], []).append(p)
        if p['key_loose']:
            by_loose.setdefault(p['key_loose'], []).append(p)
        if p['key_ascii']:
            by_ascii.setdefault(p['key_ascii'], []).append(p)

    matches_by_project = {p['path']: [] for p in projects}
    orphans = []

    for a in audios:
        # Gather candidate projects cheaply: same name, same loose name, same
        # accent-folded name, or a project whose name matches a parent folder.
        candidates = {}
        for key in (a['key_strict'], a['key_loose'], a['key_ascii'],
                    *a['folder_keys']):
            for table in (by_strict, by_loose, by_ascii):
                for p in table.get(key, ()):
                    candidates[p['path']] = p
        if not candidates:  # last resort: prefix scan
            for p in projects:
                if _prefix_match(a['key_strict'], p['key_strict']):
                    candidates[p['path']] = p

        scored = []
        for p in candidates.values():
            score, reason = score_pair(a, p)
            if score >= MATCH_THRESHOLD:
                scored.append((score, reason, p))

        if scored:
            top = max(s[0] for s in scored)
            tied = [s for s in scored if s[0] == top]
            # Break ties by whichever project's save time sits closest to the
            # render (a soft signal — cloud sync/re-saves can invert it).
            best = min(tied, key=lambda s: abs(a.get('mtime', 0)
                                               - s[2].get('mtime', 0)))
            score, reason, p = best
            rec = dict(a)
            rec['score'] = score
            rec['confidence'] = confidence_of(score)
            rec['reason'] = reason
            if len(tied) > 1:
                rec['reason'] += f'  (ambiguous: {len(tied)} projects share this name)'
                rec['ambiguous'] = True
            matches_by_project[p['path']].append(rec)
            continue

        reason = _orphan_reason(a, orphan_min_duration, include_long_unmatched)
        if reason:
            rec = dict(a)
            rec['reason'] = reason
            orphans.append(rec)

    for lst in matches_by_project.values():
        lst.sort(key=lambda r: (r['score'], r.get('mtime', 0)), reverse=True)
    orphans.sort(key=lambda r: r.get('mtime', 0), reverse=True)
    return matches_by_project, orphans


def _orphan_reason(a: dict, min_duration: float, include_long: bool) -> str:
    """Only flag an unmatched file as a *possible render* when there is
    affirmative render evidence — never on duration alone, or the bucket fills
    with the user's ordinary samples."""
    if a['ext'] not in RENDER_EXTS:
        return ''
    base = os.path.basename(os.path.normpath(a['dir'])).lower()
    in_render_folder = base in RENDER_DIR_NAMES
    dur = a.get('duration')
    long_enough = dur is not None and dur >= min_duration

    if a.get('fl_signature') and not a.get('used_as_sample'):
        return 'FL Studio metadata, no matching project'
    if in_render_folder and not a.get('used_as_sample'):
        return 'in a render folder, no matching project'
    if a.get('has_acid') and long_enough and not a.get('used_as_sample') \
            and not a.get('has_bext'):
        return 'tempo metadata on a full-length file, no matching project'
    if include_long and long_enough and not a.get('used_as_sample'):
        return f'{int(dur)}s of unmatched audio'
    return ''


if __name__ == '__main__':
    projects = [make_project_record(r'C:\FL\Projects\My Beat.flp',
                                    samples=[r'C:\Samples\Drums\kick.wav']),
                make_project_record(r'C:\FL\Projects\Untitled.flp')]
    audios = [
        make_audio_record(r'C:\FL\Projects\My Beat.wav', duration=180),
        make_audio_record(r'C:\FL\Projects\Rendered\My Beat master 140 Fmin.mp3',
                          duration=182),
        make_audio_record(r'C:\FL\Projects\My Beat\Master.wav', duration=181),
        make_audio_record(r'C:\FL\Projects\My Beat\Kick.wav', duration=181),
        make_audio_record(r'C:\Samples\Drums\kick.wav', duration=0.4,
                          used_as_sample=True),
        make_audio_record(r'C:\Music\Exports\old track.wav', duration=200,
                          has_acid=True),
    ]
    m, orphans = classify(projects, audios)
    for path, lst in m.items():
        print(path, f'-> {count_unique_renders(lst)} render(s)')
        for r in lst:
            print(f'   [{r["confidence"]}] {os.path.basename(r["path"])} — {r["reason"]}')
    print('ORPHANS:')
    for r in orphans:
        print(f'   {os.path.basename(r["path"])} — {r["reason"]}')
