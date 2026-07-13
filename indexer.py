"""Cached index of FL Studio projects, their rendered audio, and the samples
each project uses — plus search over all of it.

The cache lives at <config_dir>/index.json and stores two tables keyed by
absolute path: parsed .flp projects and probed audio files. On rescan only
files whose mtime or size changed are re-read, so everything after the first
scan is near-instant.

Performance note: a producer's audio folders can hold tens of thousands of
*samples*. We never probe them all. After listing audio files (cheap — just
directory entries), we probe only the files that are plausible renders: those
whose name or parent folder matches a project, those sitting in a render-named
folder, or — when the user opts in — everything. This keeps the first scan of a
huge sample library fast while still finding the renders hiding in it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import platform_utils as plat
import render_matcher as rm
from audio_meta import probe
from flp_parser import parse_flp

APP_DIR = plat.config_dir()
CACHE_FILE = os.path.join(APP_DIR, 'index.json')

# Bump when a cached record's shape/meaning changes: old records are discarded
# and rebuilt on the next scan.
CACHE_SCHEMA = 1

_BACKUP_DIR_NAMES = {'backup', 'autosave'}


def default_project_folders():
    return plat.default_project_folders()


def default_audio_folders():
    return plat.default_audio_folders()


def _is_backup_path(path: str) -> bool:
    parts = os.path.normpath(path).lower().split(os.sep)
    return any(part in _BACKUP_DIR_NAMES for part in parts[:-1])


class Index:
    def __init__(self, cache_file: str = CACHE_FILE):
        self.cache_file = cache_file
        self.settings = {
            'project_folders': default_project_folders(),
            'audio_folders': default_audio_folders(),
            'include_backups': False,
            'include_long_unmatched': False,
            'orphan_min_duration': 45,
            'theme': 'FL Dark',
            'manual_links': {},   # audio path (lower) -> project path, or '' to hide
        }
        self.projects = {}   # abs .flp path -> record
        self.audio = {}      # abs audio path -> probe record
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._exists_cache = {}
        self._built = None   # cached (projects, orphans) from build()
        self.load()

    def invalidate(self):
        """Drop the cached match result so the next search() rebuilds it. Call
        after changing any setting that affects matching."""
        self._built = None

    # ------------------------------------------------------------- storage

    def load(self):
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return
            saved = raw.get('settings')
            if isinstance(saved, dict):
                for key in ('project_folders', 'audio_folders'):
                    if isinstance(saved.get(key), list):
                        self.settings[key] = saved[key]
                self.settings['include_backups'] = bool(saved.get('include_backups'))
                self.settings['include_long_unmatched'] = bool(
                    saved.get('include_long_unmatched'))
                if isinstance(saved.get('orphan_min_duration'), (int, float)):
                    self.settings['orphan_min_duration'] = saved['orphan_min_duration']
                if isinstance(saved.get('theme'), str):
                    self.settings['theme'] = saved['theme']
                if isinstance(saved.get('manual_links'), dict):
                    self.settings['manual_links'] = saved['manual_links']
            if raw.get('schema') == CACHE_SCHEMA:
                if isinstance(raw.get('projects'), dict):
                    self.projects = raw['projects']
                if isinstance(raw.get('audio'), dict):
                    self.audio = raw['audio']
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # first run or corrupt cache: start fresh

    def save(self):
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        with self._lock:
            blob = json.dumps({
                'schema': CACHE_SCHEMA, 'settings': self.settings,
                'projects': self.projects, 'audio': self.audio,
            })
        with self._save_lock:
            tmp = f'{self.cache_file}.{os.getpid()}.{threading.get_ident()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(blob)
            os.replace(tmp, self.cache_file)

    # ------------------------------------------------------------- walking

    def _walk(self, folders, exts, skip_backups):
        """Yield absolute paths of files with the given extensions."""
        seen = set()
        for folder in folders:
            if not folder or not os.path.isdir(folder):
                continue
            for dirpath, dirnames, filenames in os.walk(folder):
                if skip_backups:
                    dirnames[:] = [d for d in dirnames
                                   if d.lower() not in _BACKUP_DIR_NAMES]
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in exts:
                        full = os.path.normpath(os.path.join(dirpath, name))
                        key = full.lower()
                        if key not in seen:
                            seen.add(key)
                            yield full

    # ------------------------------------------------------------- scanning

    def scan(self, progress_cb=None, cancel_event=None):
        """Re-index projects and their renders. Returns a stats dict.

        progress_cb(phase, done, total) is called during the two long phases,
        phase being 'projects' then 'renders'.
        """
        started = time.time()
        include_backups = self.settings['include_backups']
        project_folders = list(self.settings['project_folders'])
        audio_folders = list(self.settings['audio_folders'])

        parsed_projects = self._scan_projects(
            project_folders, include_backups, progress_cb, cancel_event)

        cancelled = cancel_event is not None and cancel_event.is_set()
        probed_audio = 0
        if not cancelled:
            probed_audio = self._scan_audio(
                audio_folders, include_backups, progress_cb, cancel_event)
            cancelled = cancel_event is not None and cancel_event.is_set()

        # Persist only a completed scan; a cancelled run leaves the prior cache.
        if not cancelled:
            self.save()
        self._exists_cache.clear()
        self.invalidate()
        stats = self.stats()
        stats.update({
            'parsed_projects': parsed_projects,
            'probed_audio': probed_audio,
            'cancelled': cancelled,
            'seconds': round(time.time() - started, 2),
        })
        return stats

    def _scan_projects(self, folders, include_backups, progress_cb, cancel_event):
        disk = {}
        for path in self._walk(folders, {'.flp'}, not include_backups):
            try:
                st = os.stat(path)
                disk[path] = (st.st_mtime, st.st_size)
            except OSError:
                continue

        with self._lock:
            cached = dict(self.projects)
        to_parse = [(p, m, s) for p, (m, s) in disk.items()
                    if _changed(cached.get(p), m, s)]

        # Prune projects gone from reachable roots (don't nuke a whole offline drive).
        live_roots = _live_roots(folders)
        with self._lock:
            for path in list(self.projects):
                if path.lower().startswith(live_roots) and path not in disk:
                    if include_backups or not _is_backup_path(path):
                        del self.projects[path]

        total = len(to_parse)
        done = 0
        if to_parse:
            workers = min(16, (os.cpu_count() or 4) * 2)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(parse_flp, p): (p, m, s)
                           for p, m, s in to_parse}
                for fut in as_completed(futures):
                    if cancel_event is not None and cancel_event.is_set():
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    path, mtime, size = futures[fut]
                    r = fut.result()
                    rec = {
                        'mtime': mtime, 'size': size, 'title': r.title,
                        'version': r.version, 'samples': r.samples,
                        'plugin_samples': r.plugin_samples, 'error': r.error,
                        'is_backup': _is_backup_path(path),
                    }
                    with self._lock:
                        self.projects[path] = rec
                    done += 1
                    if progress_cb:
                        progress_cb('projects', done, total)
        return done

    def _scan_audio(self, folders, include_backups, progress_cb, cancel_event):
        disk = {}
        for path in self._walk(folders, rm.RENDER_EXTS, not include_backups):
            try:
                st = os.stat(path)
                disk[path] = (st.st_mtime, st.st_size)
            except OSError:
                continue

        # Which of these files are worth probing? Only plausible renders.
        interesting = self._interesting_audio(disk)

        with self._lock:
            cached = dict(self.audio)
        to_probe = [(p, disk[p][0], disk[p][1]) for p in interesting
                    if _changed(cached.get(p), disk[p][0], disk[p][1])]

        # Rebuild the audio table from the current interesting set, reusing any
        # still-valid cached probe (drops stale/no-longer-interesting entries).
        new_audio = {}
        for p in interesting:
            m, s = disk[p]
            rec = cached.get(p)
            if rec and not _changed(rec, m, s):
                new_audio[p] = rec

        # Preserve cached entries whose audio root is currently offline, so
        # unplugging a drive doesn't wipe its renders from the index (symmetric
        # with the project pruning above).
        live = _live_roots(folders)
        configured = _roots(folders)
        for path, rec in cached.items():
            if path not in new_audio and configured and \
                    path.lower().startswith(configured) and \
                    not path.lower().startswith(live):
                new_audio[path] = rec

        total = len(to_probe)
        done = 0
        cancelled = False
        if to_probe:
            workers = min(16, (os.cpu_count() or 4) * 2)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(probe, p): (p, m, s) for p, m, s in to_probe}
                for fut in as_completed(futures):
                    if cancel_event is not None and cancel_event.is_set():
                        pool.shutdown(wait=False, cancel_futures=True)
                        cancelled = True
                        break
                    path, mtime, size = futures[fut]
                    a = fut.result()
                    new_audio[path] = {
                        'mtime': mtime, 'size': size, 'duration': a.duration,
                        'codec': a.codec, 'sample_rate': a.sample_rate,
                        'channels': a.channels, 'bit_depth': a.bit_depth,
                        'encoder': a.encoder, 'fl_signature': a.fl_signature,
                        'has_acid': a.has_acid, 'has_bext': a.has_bext,
                        'error': a.error,
                    }
                    done += 1
                    if progress_cb:
                        progress_cb('renders', done, total)
        # Don't overwrite the cache with a partial view if the scan was aborted.
        if not cancelled:
            with self._lock:
                self.audio = new_audio
        return done

    def _interesting_audio(self, disk):
        """Set of audio paths worth probing: name/folder candidates of some
        project, files in render-named folders, or everything when opted in."""
        include_long = self.settings['include_long_unmatched']
        projects = self._project_records()
        by_strict, by_loose, by_ascii = _key_tables(projects)
        interesting = set()
        for path in disk:
            base_dir = os.path.basename(os.path.dirname(path)).lower()
            if include_long or base_dir in rm.RENDER_DIR_NAMES:
                interesting.add(path)
                continue
            a = rm.make_audio_record(path)
            if _has_candidate(a, by_strict, by_loose, by_ascii, projects):
                interesting.add(path)
        return interesting

    # ------------------------------------------------------------- records

    def _in_scope_project(self, path, rec):
        roots = _roots(self.settings['project_folders'])
        if not path.lower().startswith(roots):
            return False
        if not self.settings['include_backups'] and rec.get('is_backup'):
            return False
        return True

    def _project_records(self):
        with self._lock:
            items = [(p, r) for p, r in self.projects.items()
                     if self._in_scope_project(p, r)]
        recs = []
        for path, r in items:
            samples = list(r.get('samples', ())) + list(r.get('plugin_samples', ()))
            rec = rm.make_project_record(path, samples=samples,
                                         mtime=r.get('mtime', 0))
            rec.update({'title': r.get('title', ''), 'version': r.get('version', ''),
                        'error': r.get('error', ''), 'is_backup': r.get('is_backup', False),
                        'samples': r.get('samples', []),
                        'plugin_samples': r.get('plugin_samples', [])})
            recs.append(rec)
        return recs

    def _sample_basenames(self):
        names = set()
        with self._lock:
            for r in self.projects.values():
                for s in list(r.get('samples', ())) + list(r.get('plugin_samples', ())):
                    base = s.replace('/', '\\').rsplit('\\', 1)[-1]
                    if base:
                        names.add(base.lower())
        return names

    def _audio_records(self):
        sample_names = self._sample_basenames()
        with self._lock:
            items = list(self.audio.items())
        recs = []
        for path, r in items:
            rec = rm.make_audio_record(
                path,
                duration=r.get('duration'),
                used_as_sample=os.path.basename(path).lower() in sample_names,
                fl_signature=r.get('fl_signature', False),
                has_acid=r.get('has_acid', False),
                has_bext=r.get('has_bext', False),
                mtime=r.get('mtime', 0),
            )
            rec.update({'size': r.get('size', 0), 'codec': r.get('codec', ''),
                        'sample_rate': r.get('sample_rate'),
                        'channels': r.get('channels'),
                        'encoder': r.get('encoder', ''), 'error': r.get('error', '')})
            recs.append(rec)
        return recs

    # --------------------------------------------------------------- build

    def build(self):
        """Run the matcher over the current caches. Returns (projects, orphans)
        where projects is a list of project result dicts (each with its renders
        attached) and orphans is a list of unmatched-render records. The result
        is cached until invalidate() is called (scan or settings change)."""
        if self._built is not None:
            return self._built
        projects = self._project_records()
        audios = self._audio_records()
        matches, orphans = rm.classify(
            projects, audios,
            orphan_min_duration=self.settings['orphan_min_duration'],
            include_long_unmatched=self.settings['include_long_unmatched'])
        matches, orphans = self._apply_manual_links(projects, matches, orphans)

        results = []
        by_path = {p['path']: p for p in projects}
        for path, renders in matches.items():
            p = by_path[path]
            results.append({
                'path': path, 'name': os.path.basename(path),
                'title': p.get('title', ''), 'version': p.get('version', ''),
                'mtime': p.get('mtime', 0), 'error': p.get('error', ''),
                'is_backup': p.get('is_backup', False),
                'samples': p.get('samples', []),
                'plugin_samples': p.get('plugin_samples', []),
                'renders': renders,
                'render_count': rm.count_unique_renders(renders),
            })
        self._built = (results, orphans)
        return self._built

    def _apply_manual_links(self, projects, matches, orphans):
        """Honor user overrides: force an audio file under a chosen project, or
        hide it entirely ('' target)."""
        links = self.settings.get('manual_links') or {}
        if not links:
            return matches, orphans
        by_path = {p['path']: p for p in projects}

        for audio_lower, target in list(links.items()):
            # locate the render record wherever it currently sits
            rec = None
            loc = None
            for plist in matches.values():
                for i, r in enumerate(plist):
                    if r['path'].lower() == audio_lower:
                        rec, loc = r, (plist, i)
                        break
                if rec:
                    break
            if rec is None:
                for r in orphans:
                    if r['path'].lower() == audio_lower:
                        rec = r
                        break
            if rec is None:
                continue
            # detach from wherever it is
            if loc:
                loc[0].pop(loc[1])
            elif rec in orphans:
                orphans.remove(rec)
            if target and target in by_path:
                rec = dict(rec)
                rec['confidence'] = 'manual'
                rec['reason'] = 'manually linked'
                rec.setdefault('score', 100)
                matches[target].append(rec)
            elif target:
                # Target project isn't currently in scope (folder removed, drive
                # offline) — keep the render visible as an orphan rather than
                # silently dropping it.
                orphans.append(rec)
            # target == '' -> hidden (dropped entirely)
        for lst in matches.values():
            lst.sort(key=lambda r: (r.get('score', 0), r.get('mtime', 0)), reverse=True)
        return matches, orphans

    def set_manual_link(self, audio_path, project_path):
        """project_path='' hides the file; None removes any override."""
        links = self.settings.setdefault('manual_links', {})
        key = audio_path.lower()
        if project_path is None:
            links.pop(key, None)
        else:
            links[key] = project_path
        self.invalidate()
        self.save()

    # ---------------------------------------------------------- statistics

    def stats(self):
        with self._lock:
            projs = [(p, r) for p, r in self.projects.items()
                     if self._in_scope_project(p, r)]
            audio_n = len(self.audio)
        unique_samples = set()
        for _, r in projs:
            unique_samples.update(s.lower() for s in r.get('samples', ()))
            unique_samples.update(s.lower() for s in r.get('plugin_samples', ()))
        return {'projects': len(projs), 'audio_indexed': audio_n,
                'unique_samples': len(unique_samples)}

    # ------------------------------------------------- sample existence

    def _sample_exists(self, sample, factory_dirs):
        resolved = plat.resolve_fl_path(sample, factory_dirs)
        if resolved is None:
            return None
        cached = self._exists_cache.get(resolved)
        if cached is None:
            try:
                cached = os.path.exists(resolved)
            except OSError:
                cached = False
            self._exists_cache[resolved] = cached
        return cached

    def missing_samples(self, samples, factory_dirs=None):
        if factory_dirs is None:
            factory_dirs = plat.fl_factory_data_dirs()
        return [s for s in samples
                if self._sample_exists(s, factory_dirs) is False]

    def clear_exists_cache(self):
        self._exists_cache.clear()

    # -------------------------------------------------------------- search

    def search(self, query='', only_with_renders=True, include_orphans=True):
        """Return (project_results, orphan_results) filtered by the query.

        A project matches if any term appears in its file name/title, in one of
        its render file names, or in one of its sample names. An orphan matches
        on its file name.
        """
        projects, orphans = self.build()
        terms = [t for t in query.lower().split() if t]

        def proj_matches(p):
            if not terms:
                return True
            hay = [p['name'].lower(), p['title'].lower()]
            hay += [os.path.basename(r['path']).lower() for r in p['renders']]
            hay += [s.lower() for s in p['samples']]
            hay += [s.lower() for s in p['plugin_samples']]
            return all(any(t in h for h in hay) for t in terms)

        out = []
        for p in projects:
            if only_with_renders and not p['renders']:
                continue
            if proj_matches(p):
                out.append(p)
        out.sort(key=lambda p: (bool(p['renders']), p['render_count'], p['mtime']),
                 reverse=True)

        orphan_out = []
        if include_orphans:
            for r in orphans:
                if not terms or all(t in os.path.basename(r['path']).lower()
                                    for t in terms):
                    orphan_out.append(r)
        return out, orphan_out


# --------------------------------------------------------------------------
# module helpers
# --------------------------------------------------------------------------

def _changed(rec, mtime, size):
    return (rec is None or rec.get('mtime') != mtime or rec.get('size') != size
            or str(rec.get('error', '')).startswith('unreadable'))


def _roots(folders):
    return tuple(os.path.normpath(f).lower() + os.sep for f in folders if f)


def _live_roots(folders):
    return tuple(os.path.normpath(f).lower() + os.sep
                 for f in folders if os.path.isdir(f))


def _key_tables(projects):
    by_strict, by_loose, by_ascii = {}, {}, {}
    for p in projects:
        by_strict.setdefault(p['key_strict'], []).append(p)
        if p['key_loose']:
            by_loose.setdefault(p['key_loose'], []).append(p)
        if p['key_ascii']:
            by_ascii.setdefault(p['key_ascii'], []).append(p)
    return by_strict, by_loose, by_ascii


def _has_candidate(a, by_strict, by_loose, by_ascii, projects):
    for key in (a['key_strict'], a['key_loose'], a['key_ascii'], *a['folder_keys']):
        if key and (key in by_strict or key in by_loose or key in by_ascii):
            return True
    for p in projects:
        if rm._prefix_match(a['key_strict'], p['key_strict']):
            return True
    return False


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='FLIndexRenders CLI (for testing)')
    ap.add_argument('--scan', action='store_true')
    ap.add_argument('--query', default='')
    ap.add_argument('--all', action='store_true', help='include projects without renders')
    ap.add_argument('--cache', default=CACHE_FILE)
    args = ap.parse_args()

    index = Index(cache_file=args.cache)
    if args.scan:
        stats = index.scan(progress_cb=lambda ph, d, t: print(
            f'\r  {ph}: {d}/{t}   ', end='', flush=True))
        print('\nscan:', stats)
    projects, orphans = index.search(args.query, only_with_renders=not args.all)
    print(f'\n{len(projects)} project(s):')
    for p in projects[:40]:
        print(f'  {p["name"]}  —  {p["render_count"]} render(s)')
        for r in p['renders'][:8]:
            dur = f'{r["duration"]:.0f}s' if r.get('duration') else '?'
            print(f'      [{r["confidence"]}] {os.path.basename(r["path"])} '
                  f'({r.get("codec","?")}, {dur}) — {r["reason"]}')
    if orphans:
        print(f'\n{len(orphans)} possible render(s) with no project:')
        for r in orphans[:20]:
            print(f'  {os.path.basename(r["path"])} — {r["reason"]}')
