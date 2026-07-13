"""Cross-platform helpers so the app runs the same on Windows, macOS and Linux.

Everything OS-specific lives here: where config is stored, how to open a file
in its default app (a render in the audio player, a project in FL Studio), how
to reveal a file in the file manager, where FL Studio keeps projects and
renders, and how to expand FL's %macro% path variables.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys

APP_NAME = 'FLIndexRenders'

IS_WINDOWS = sys.platform.startswith('win')
IS_MAC = sys.platform == 'darwin'


def config_dir() -> str:
    """Per-user writable config directory, created if missing."""
    if IS_WINDOWS:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    elif IS_MAC:
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _documents_dirs() -> list:
    home = os.path.expanduser('~')
    dirs = [os.path.join(home, 'Documents')]
    # OneDrive relocates Documents on many Windows setups.
    for var in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        base = os.environ.get(var)
        if base:
            dirs.append(os.path.join(base, 'Documents'))
    if os.environ.get('USERPROFILE'):
        dirs.append(os.path.join(os.environ['USERPROFILE'], 'Documents'))
    # De-dup, keep order.
    seen, out = set(), []
    for d in dirs:
        k = os.path.normcase(os.path.normpath(d))
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _music_dirs() -> list:
    home = os.path.expanduser('~')
    dirs = [os.path.join(home, 'Music')]
    if os.environ.get('USERPROFILE'):
        dirs.append(os.path.join(os.environ['USERPROFILE'], 'Music'))
    seen, out = set(), []
    for d in dirs:
        k = os.path.normcase(os.path.normpath(d))
        if k not in seen and os.path.isdir(d):
            seen.add(k)
            out.append(os.path.normpath(d))
    return out


def default_project_folders() -> list:
    """Best-effort auto-detection of the FL Studio Projects folder(s).

    This is where .flp projects live and, by default, where FL Studio drops
    renders too (the render dialog defaults to the project's own folder).
    """
    found = []
    for docs in _documents_dirs():
        cand = os.path.join(docs, 'Image-Line', 'FL Studio', 'Projects')
        if os.path.isdir(cand):
            found.append(os.path.normpath(cand))
    return found


def default_audio_folders() -> list:
    """Folders scanned for rendered audio. Defaults to the FL Projects folder
    (renders sit next to their projects by default) plus the user's Music
    folder. Users add their own render/export folders from the Folders dialog.
    """
    found = list(default_project_folders())
    found += _music_dirs()
    seen, out = set(), []
    for d in found:
        k = os.path.normcase(os.path.normpath(d))
        if k not in seen:
            seen.add(k)
            out.append(os.path.normpath(d))
    return out


def fl_factory_data_dirs() -> list:
    """Locate FL Studio's factory Data folder(s), newest first (best effort)."""
    roots = []
    if IS_WINDOWS:
        for pf in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'),
                   r'C:\Program Files', r'C:\Program Files (x86)'):
            if pf:
                roots += glob.glob(os.path.join(pf, 'Image-Line', 'FL Studio*', 'Data'))
    elif IS_MAC:
        roots += glob.glob('/Applications/FL Studio*.app/Contents/Resources/FL/Data')
        roots += glob.glob('/Applications/Image-Line/FL Studio*/Data')
    return sorted({os.path.normpath(r) for r in roots if os.path.isdir(r)}, reverse=True)


def _expand_env_style(path: str) -> str:
    # FL writes %VAR% on every platform; on non-Windows os.path.expandvars
    # doesn't handle %VAR%, so normalize to $VAR first.
    out = path
    if '%' in out:
        import re
        out = re.sub(r'%([^%]+)%', r'${\1}', out)
    return os.path.expandvars(out)


def resolve_fl_path(path: str, factory_dirs=None) -> str | None:
    """Expand FL's %macro% path variables to a concrete filesystem path.

    Returns a resolvable absolute path, or None if it can't be resolved (e.g. a
    factory macro on a machine where FL isn't installed) — callers should treat
    None as 'unknown', not 'missing'.
    """
    if not path:
        return None
    raw = path.strip()
    low = raw.lower()

    macro_map = {'%userprofile%': os.path.expanduser('~')}
    # %FLStudioUserData% -> the "...\Image-Line\FL Studio" folder.
    for docs in _documents_dirs():
        fl_user = os.path.join(docs, 'Image-Line', 'FL Studio')
        if os.path.isdir(fl_user):
            macro_map['%flstudiouserdata%'] = fl_user
            break

    if factory_dirs is None:
        factory_dirs = fl_factory_data_dirs()

    if low.startswith('%flstudiofactorydata%'):
        rest = raw[len('%FLStudioFactoryData%'):].lstrip('\\/')
        parts = rest.replace('\\', '/').split('/')
        for fac in factory_dirs:
            # FL writes the macro as either the install root ("...\Data\...")
            # or the Data dir itself, so try the Data folder and its parent.
            for base in (os.path.dirname(fac), fac):
                cand = os.path.normpath(os.path.join(base, *parts))
                if os.path.exists(cand):
                    return cand
        return None  # FL not installed / unknown location

    for macro, target in macro_map.items():
        if target and low.startswith(macro):
            rest = raw[len(macro):].lstrip('\\/')
            return os.path.normpath(os.path.join(target, *rest.replace('\\', '/').split('/')))

    if raw.startswith('%'):
        expanded = _expand_env_style(raw)
        if '%' not in expanded and '$' not in expanded:
            return os.path.normpath(expanded)
        return None  # unknown macro

    # A plain path. On the wrong OS a Windows-style path won't resolve; that's
    # fine — we simply report it can't be confirmed.
    return os.path.normpath(raw)


def open_file(path: str):
    """Open a file/folder in its default application (audio player, FL Studio)."""
    if IS_WINDOWS:
        os.startfile(path)  # noqa: E1101 (Windows-only)
    elif IS_MAC:
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


def reveal_file(path: str):
    """Reveal a file in the OS file manager, selected if possible."""
    path = os.path.normpath(path)
    exists = os.path.exists(path)
    if IS_WINDOWS:
        if exists:
            subprocess.Popen(['explorer', '/select,', path])
        else:
            subprocess.Popen(['explorer', os.path.dirname(path)])
    elif IS_MAC:
        if exists:
            subprocess.Popen(['open', '-R', path])
        else:
            subprocess.Popen(['open', os.path.dirname(path)])
    else:
        target = path if exists else os.path.dirname(path)
        subprocess.Popen(['xdg-open', target])
