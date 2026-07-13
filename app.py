"""FLIndexRenders — find every render FL Studio ever exported, treed by project.

Renders (your exported/bounced tracks) get lost among the thousands of samples
on a producer's drive — same file types, unreliable dates. This indexes your FL
Studio projects and the audio around them, groups each render under the project
it came from, and shows the samples that went into it.

Developed by ajh — https://ajh.wtf
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import platform_utils as plat
from indexer import Index

APP_TITLE = 'FLIndexRenders'
CREDIT_TEXT = 'by ajh · ajh.wtf'
CREDIT_URL = 'https://ajh.wtf'

FONT = ('Segoe UI', 10) if plat.IS_WINDOWS else ('Helvetica', 12)
FONT_SEARCH = ('Segoe UI', 13) if plat.IS_WINDOWS else ('Helvetica', 15)
FONT_MONO = ('Consolas', 10) if plat.IS_WINDOWS else ('Menlo', 11)

# -- theme palettes (shared look with the sister tool FLSearchBySample) ----
THEMES = {
    'FL Dark': {
        'bg': '#1e2024', 'panel': '#26282e', 'field': '#2d3038', 'fg': '#e6e6e6',
        'dim': '#9aa0a8', 'accent': '#ff9f43', 'select': '#3a4a63',
        'sel_fg': '#ffffff', 'warn': '#ff6b6b', 'good': '#7bd88f',
    },
    'Midnight': {
        'bg': '#0f1420', 'panel': '#161d2e', 'field': '#1c2740', 'fg': '#e7ecf5',
        'dim': '#8794ad', 'accent': '#38bdf8', 'select': '#24406b',
        'sel_fg': '#ffffff', 'warn': '#ff7a90', 'good': '#5fd0a6',
    },
    'Graphite': {
        'bg': '#202124', 'panel': '#2a2b2e', 'field': '#35363a', 'fg': '#e8eaed',
        'dim': '#9aa0a6', 'accent': '#8ab4f8', 'select': '#3c4043',
        'sel_fg': '#ffffff', 'warn': '#f28b82', 'good': '#81c995',
    },
    'Light': {
        'bg': '#f3f4f6', 'panel': '#ffffff', 'field': '#ffffff', 'fg': '#1f2430',
        'dim': '#6b7280', 'accent': '#ea6a1e', 'select': '#cfe0ff',
        'sel_fg': '#10233f', 'warn': '#c0392b', 'good': '#1a7f43',
    },
    'High Contrast': {
        'bg': '#000000', 'panel': '#0b0b0b', 'field': '#151515', 'fg': '#ffffff',
        'dim': '#b8b8b8', 'accent': '#ffd000', 'select': '#00509e',
        'sel_fg': '#ffffff', 'warn': '#ff5252', 'good': '#69f0ae',
    },
}
DEFAULT_THEME = 'FL Dark'


def resource_path(rel):
    """Path to a bundled data file, working both from source and a frozen exe."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _fmt_duration(seconds):
    if not seconds or seconds <= 0:
        return ''
    seconds = int(round(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def _fmt_size(n):
    if not n:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return ''


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        width = min(1180, self.winfo_screenwidth() - 60)
        height = min(760, self.winfo_screenheight() - 120)
        self.geometry(f'{width}x{height}')
        self.minsize(720, 500)

        self.index = Index()
        self._projects = []
        self._orphans = []
        self._item_map = {}          # tree iid -> ('project'|'render'|'orphangroup', ...)
        self._scan_queue = queue.Queue()
        self._scanning = False
        self._scan_pending = False
        self._search_job = None
        self._tk_widgets = []

        theme_name = self.index.settings.get('theme', DEFAULT_THEME)
        self.theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
        self.theme_var = tk.StringVar(value=theme_name if theme_name in THEMES
                                      else DEFAULT_THEME)

        self._set_window_icon()
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self._build_menu()
        self._build_ui()
        self.apply_theme(self.theme_var.get())

        self.refresh_results()
        self.after(200, self.start_scan)

    def _set_window_icon(self):
        try:
            self._icon_img = tk.PhotoImage(file=resource_path('assets/icon.png'))
            self.iconphoto(True, self._icon_img)
        except Exception:
            pass
        if plat.IS_WINDOWS:
            try:
                self.iconbitmap(resource_path('assets/icon.ico'))
            except Exception:
                pass

    # ----------------------------------------------------------------- menu

    def _build_menu(self):
        menubar = tk.Menu(self)

        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label='Export sample list…', command=self.export_selected)
        filem.add_command(label='Rescan', command=self.start_scan)
        filem.add_separator()
        filem.add_command(label='Quit', command=self.destroy)
        menubar.add_cascade(label='File', menu=filem)

        viewm = tk.Menu(menubar, tearoff=0)
        viewm.add_command(label='Expand all', command=lambda: self._set_all_open(True))
        viewm.add_command(label='Collapse all', command=lambda: self._set_all_open(False))
        viewm.add_separator()
        thememenu = tk.Menu(viewm, tearoff=0)
        for name in THEMES:
            thememenu.add_radiobutton(label=name, value=name,
                                      variable=self.theme_var,
                                      command=lambda: self.apply_theme(self.theme_var.get()))
        viewm.add_cascade(label='Theme', menu=thememenu)
        menubar.add_cascade(label='View', menu=viewm)
        self._menu_children = [menubar, filem, viewm, thememenu]

        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label=f'About {APP_TITLE}…', command=self.show_about)
        helpm.add_command(label='Visit ajh.wtf', command=lambda: webbrowser.open(CREDIT_URL))
        menubar.add_cascade(label='Help', menu=helpm)
        self._menu_children.append(helpm)

        self.config(menu=menubar)

    # ------------------------------------------------------------------- UI

    def _build_ui(self):
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill='x')
        ttk.Label(top, text='Search:').pack(side='left')
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', self._on_search_changed)
        self.search_entry = ttk.Entry(top, textvariable=self.search_var, font=FONT_SEARCH)
        self.search_entry.pack(side='left', fill='x', expand=True, padx=(8, 8))
        self.search_entry.focus_set()
        self.search_entry.bind('<Escape>', lambda e: self.search_var.set(''))
        self.rescan_btn = ttk.Button(top, text='Rescan', command=self.start_scan)
        self.rescan_btn.pack(side='right')
        ttk.Button(top, text='Folders…', command=self.open_folders_dialog
                   ).pack(side='right', padx=(0, 8))

        opts = ttk.Frame(self, padding=(10, 0, 10, 2))
        opts.pack(fill='x')
        self.only_renders_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text='Only projects with renders',
                        variable=self.only_renders_var,
                        command=self.refresh_results).pack(side='left')
        self.backups_var = tk.BooleanVar(
            value=self.index.settings.get('include_backups', False))
        ttk.Checkbutton(opts, text='Include autosave backups',
                        variable=self.backups_var,
                        command=self._on_backups_toggled).pack(side='left', padx=(14, 0))
        self.long_var = tk.BooleanVar(
            value=self.index.settings.get('include_long_unmatched', False))
        ttk.Checkbutton(opts, text='List long unmatched audio as possible renders',
                        variable=self.long_var,
                        command=self._on_long_toggled).pack(side='left', padx=(14, 0))

        prog = ttk.Frame(self, padding=(10, 0, 10, 4))
        prog.pack(fill='x')
        self.status_var = tk.StringVar(value='Loading…')
        ttk.Label(prog, textvariable=self.status_var, style='Dim.TLabel').pack(side='left')
        self.progress = ttk.Progressbar(prog, length=220, mode='determinate')

        paned = ttk.PanedWindow(self, orient='vertical')
        paned.pack(fill='both', expand=True, padx=10, pady=(2, 6))

        table_frame = ttk.Frame(paned)
        columns = ('detail', 'duration', 'modified', 'location')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='tree headings',
                                 selectmode='browse')
        self.tree.heading('#0', text='Project  /  render')
        self.tree.heading('detail', text='Detail')
        self.tree.heading('duration', text='Length')
        self.tree.heading('modified', text='Modified')
        self.tree.heading('location', text='Location')
        self.tree.column('#0', width=340, stretch=True)
        self.tree.column('detail', width=230, stretch=False)
        self.tree.column('duration', width=80, anchor='e', stretch=False)
        self.tree.column('modified', width=130, stretch=False)
        self.tree.column('location', width=320, stretch=True)
        yscroll = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        yscroll.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Double-1>', self._on_double_click)
        self.tree.bind('<Return>', lambda e: self._activate_selected())
        self.tree.bind('<Button-3>', self._on_right_click)
        if plat.IS_MAC:
            self.tree.bind('<Button-2>', self._on_right_click)
            self.tree.bind('<Control-Button-1>', self._on_right_click)
        paned.add(table_frame, weight=3)

        detail_frame = ttk.Frame(paned)
        self.detail_header = tk.StringVar(value=self._DETAIL_EMPTY)
        ttk.Label(detail_frame, textvariable=self.detail_header,
                  style='Dim.TLabel').pack(anchor='w', pady=(4, 2))
        self.detail = tk.Listbox(detail_frame, font=FONT_MONO, highlightthickness=0,
                                 borderwidth=0, activestyle='none')
        self._tk_widgets.append(self.detail)
        dscroll = ttk.Scrollbar(detail_frame, orient='vertical', command=self.detail.yview)
        self.detail.configure(yscrollcommand=dscroll.set)
        self.detail.pack(side='left', fill='both', expand=True)
        dscroll.pack(side='right', fill='y')
        paned.add(detail_frame, weight=2)

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill='x')
        self.play_btn = ttk.Button(bottom, text='▶  Play render', command=self.play_selected)
        self.play_btn.pack(side='left')
        ttk.Button(bottom, text='Open in FL Studio', command=self.open_project
                   ).pack(side='left', padx=8)
        ttk.Button(bottom, text='Open folder', command=self.reveal_selected
                   ).pack(side='left')
        ttk.Button(bottom, text='Export sample list…', command=self.export_selected
                   ).pack(side='left', padx=8)
        self.credit = ttk.Label(bottom, text=CREDIT_TEXT, style='Credit.TLabel',
                                cursor='hand2')
        self.credit.pack(side='right')
        self.credit.bind('<Button-1>', lambda e: webbrowser.open(CREDIT_URL))

        self._context = tk.Menu(self, tearoff=0)
        self._menu_children.append(self._context)

    # ------------------------------------------------------------- theming

    def apply_theme(self, name):
        c = THEMES.get(name, THEMES[DEFAULT_THEME])
        self.theme = c
        self.index.settings['theme'] = name
        self.index.save()
        s = self.style
        s.configure('.', background=c['bg'], foreground=c['fg'],
                    fieldbackground=c['field'], font=FONT, borderwidth=0)
        s.configure('TFrame', background=c['bg'])
        s.configure('TLabel', background=c['bg'], foreground=c['fg'])
        s.configure('Dim.TLabel', background=c['bg'], foreground=c['dim'])
        s.configure('Credit.TLabel', background=c['bg'], foreground=c['accent'])
        s.configure('TButton', background=c['panel'], foreground=c['fg'], padding=(10, 5))
        s.map('TButton', background=[('active', c['select']), ('pressed', c['select'])])
        s.configure('TCheckbutton', background=c['bg'], foreground=c['fg'])
        s.map('TCheckbutton', background=[('active', c['bg'])],
              foreground=[('active', c['fg'])])
        s.configure('TEntry', insertcolor=c['fg'], fieldbackground=c['field'],
                    foreground=c['fg'], padding=6)
        s.configure('TPanedwindow', background=c['bg'])
        s.configure('Treeview', background=c['panel'], foreground=c['fg'],
                    fieldbackground=c['panel'], rowheight=24)
        s.map('Treeview', background=[('selected', c['select'])],
              foreground=[('selected', c['sel_fg'])])
        s.configure('Treeview.Heading', background=c['field'], foreground=c['fg'],
                    padding=(6, 4), relief='flat')
        s.map('Treeview.Heading', background=[('active', c['select'])])
        s.configure('Horizontal.TProgressbar', background=c['accent'], troughcolor=c['field'])
        s.configure('TScrollbar', background=c['panel'], troughcolor=c['bg'],
                    arrowcolor=c['dim'])

        self.tree.tag_configure('project', foreground=c['fg'])
        self.tree.tag_configure('render', foreground=c['fg'])
        self.tree.tag_configure('render_low', foreground=c['dim'])
        self.tree.tag_configure('orphangroup', foreground=c['accent'])
        self.tree.tag_configure('orphan', foreground=c['dim'])
        self.tree.tag_configure('empty', foreground=c['dim'])

        self.configure(bg=c['bg'])
        for w in self._tk_widgets:
            w.configure(bg=c['panel'], fg=c['fg'], selectbackground=c['select'],
                        selectforeground=c['sel_fg'])
        for m in getattr(self, '_menu_children', []):
            try:
                m.configure(bg=c['panel'], fg=c['fg'], activebackground=c['select'],
                            activeforeground=c['sel_fg'], selectcolor=c['accent'])
            except tk.TclError:
                pass
        self._on_select()

    # ------------------------------------------------------------- search

    def _on_search_changed(self, *_):
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(250, self.refresh_results)

    def refresh_results(self):
        if self._search_job is not None:
            self.after_cancel(self._search_job)
            self._search_job = None
        query = self.search_var.get()
        self._projects, self._orphans = self.index.search(
            query, only_with_renders=self.only_renders_var.get())
        self._populate_tree(searching=bool(query.strip()))
        self._update_status()

    def _populate_tree(self, searching=False):
        self.tree.delete(*self.tree.get_children())
        self._item_map.clear()

        for p in self._projects:
            n = p['render_count']
            detail = (f'{n} render{"s" if n != 1 else ""}' if n
                      else 'no renders found')
            if p['version']:
                detail += f'   ·  FL {p["version"]}'
            if p['error']:
                detail += f'   ·  [{p["error"]}]'
            when = time.strftime('%Y-%m-%d %H:%M', time.localtime(p['mtime']))
            label = os.path.splitext(p['name'])[0]
            if p['title'] and p['title'].lower() != label.lower():
                label += f'   “{p["title"]}”'
            if p['is_backup']:
                label += '  (backup)'
            tag = 'project' if n else 'empty'
            pid = self.tree.insert('', 'end', text='📁  ' + label,
                                   values=(detail, '', when, os.path.dirname(p['path'])),
                                   open=searching, tags=(tag,))
            self._item_map[pid] = ('project', p)
            for r in p['renders']:
                self._insert_render(pid, r, p, project_dir=os.path.dirname(p['path']))

        if self._orphans:
            oid = self.tree.insert(
                '', 'end', tags=('orphangroup',), open=searching,
                text=f'🔎  Renders with no matching project  ({len(self._orphans)})',
                values=('unlinked audio that looks like a render', '', '', ''))
            self._item_map[oid] = ('orphangroup', self._orphans)
            for r in self._orphans:
                self._insert_render(oid, r, None, project_dir=None)

        if not self.tree.get_children():
            self.tree.insert('', 'end', text='  (nothing to show — try Rescan or add '
                             'folders)', tags=('empty',))

        kids = self.tree.get_children()
        selected = False
        if kids and self._item_map:
            first = next((k for k in kids if k in self._item_map), None)
            if first:
                self.tree.selection_set(first)
                selected = True
        if not selected:
            # No selectable rows (e.g. a search with no hits): reset the detail
            # pane and Play button instead of leaving the last item's state.
            self._on_select()

    def _insert_render(self, parent, r, project, project_dir):
        conf = r.get('confidence', '')
        codec = (r.get('codec') or os.path.splitext(r['path'])[1].lstrip('.')).upper()
        detail = codec
        if r.get('sample_rate'):
            detail += f'  {r["sample_rate"] / 1000:g}kHz'
        if conf and conf not in ('high', 'manual'):
            detail += f'  · {conf} confidence'
        elif conf == 'manual':
            detail += '  · linked'
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(r.get('mtime', 0)))
        loc = os.path.dirname(r['path'])
        if project_dir and os.path.normcase(loc) == os.path.normcase(project_dir):
            loc = '(same folder)'
        tag = 'orphan' if project is None else (
            'render' if conf in ('high', 'manual') else 'render_low')
        rid = self.tree.insert(parent, 'end', text='   🎵  ' + os.path.basename(r['path']),
                               values=(detail, _fmt_duration(r.get('duration')), when, loc),
                               tags=(tag,))
        self._item_map[rid] = ('render', r, project)

    def _set_all_open(self, state):
        for iid in self.tree.get_children():
            self.tree.item(iid, open=state)

    def _update_status(self, scan_note=''):
        stats = self.index.stats()
        parts = []
        n_renders = sum(p['render_count'] for p in self._projects)
        if self.search_var.get().strip():
            parts.append(f'{len(self._projects)} matching project(s)')
        parts.append(f'{n_renders} renders in {len(self._projects)} project(s)')
        if self._orphans:
            parts.append(f'{len(self._orphans)} unlinked')
        parts.append(f"{stats['projects']} projects · {stats['audio_indexed']} audio indexed")
        if scan_note:
            parts.append(scan_note)
        self.status_var.set('   ·   '.join(parts))

    # ------------------------------------------------------------ details

    _DETAIL_EMPTY = 'Select a project or render to see the samples that went into it.'

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self._item_map.get(sel[0])

    def _selected_render(self):
        item = self._selected()
        if item and item[0] == 'render':
            return item[1], item[2]
        return None, None

    def _selected_project(self):
        item = self._selected()
        if not item:
            return None
        if item[0] == 'project':
            return item[1]
        if item[0] == 'render':
            return item[2]
        return None

    def _on_select(self, _event=None):
        self.detail.delete(0, 'end')
        item = self._selected()
        self.play_btn.state(['!disabled'] if (item and item[0] == 'render')
                            else ['disabled'])
        if not item:
            self.detail_header.set(self._DETAIL_EMPTY)
            return
        if item[0] == 'orphangroup':
            self.detail_header.set(
                'Audio that looks like a render but matches no indexed project. '
                'Right-click one to link it to a project.')
            return
        render = item[1] if item[0] == 'render' else None
        project = self._selected_project()

        if render is not None and project is None:  # orphan render
            self._fill_render_meta(render)
            return
        if project is None:
            self.detail_header.set(self._DETAIL_EMPTY)
            return
        self._fill_project_samples(project, render)

    def _fill_render_meta(self, r):
        bits = [os.path.basename(r['path'])]
        if r.get('duration'):
            bits.append(_fmt_duration(r['duration']))
        if r.get('codec'):
            bits.append(r['codec'].upper())
        if r.get('size'):
            bits.append(_fmt_size(r['size']))
        self.detail_header.set('   ·   '.join(bits))
        self.detail.insert('end', f'  {r.get("reason", "no matching project")}')
        self.detail.insert('end', '')
        for k, label in (('sample_rate', 'sample rate'), ('channels', 'channels'),
                         ('encoder', 'encoder'), ('has_acid', 'FL tempo chunk'),
                         ('has_bext', 'broadcast-wave chunk')):
            v = r.get(k)
            if v:
                if k == 'sample_rate':
                    v = f'{v} Hz'
                self.detail.insert('end', f'  {label}: {v}')

    def _fill_project_samples(self, project, render):
        samples = project.get('samples', [])
        plugin = project.get('plugin_samples', [])
        missing = {m.lower() for m in self.index.missing_samples(samples + plugin)}
        bits = [project['name']]
        if project.get('title'):
            bits.append(f'“{project["title"]}”')
        if render is not None:
            bits.append(f'render: {os.path.basename(render["path"])}')
            if render.get('reason'):
                bits.append(render['reason'])
        n = len(samples) + len(plugin)
        bits.append(f'{n} sample{"s" if n != 1 else ""}')
        if missing:
            bits.append(f'{len(missing)} missing on disk')
        self.detail_header.set('   ·   '.join(bits))
        warn_idx = []
        row = 0
        if not (samples or plugin):
            self.detail.insert('end', '  (no samples referenced by this project)')
            return
        for tag, items in (('', samples), ('[plugin] ', plugin)):
            for sname in items:
                miss = '   ✗ MISSING' if sname.lower() in missing else ''
                self.detail.insert('end', f'  {tag}{sname}{miss}')
                if miss:
                    warn_idx.append(row)
                row += 1
        for i in warn_idx:
            self.detail.itemconfig(i, foreground=self.theme['warn'])

    # ------------------------------------------------------------ actions

    def _on_double_click(self, event):
        # Double-clicking a render plays it; leave projects/groups to Tk's
        # default expand/collapse so an accidental double-click doesn't launch
        # FL Studio.
        row = self.tree.identify_row(event.y)
        item = self._item_map.get(row)
        if item and item[0] == 'render':
            self.play_selected()
            return 'break'

    def _activate_selected(self):
        item = self._selected()
        if not item:
            return
        if item[0] == 'render':
            self.play_selected()
        elif item[0] == 'project':
            self.open_project()

    def play_selected(self):
        r, _ = self._selected_render()
        if r is None:
            return
        if not os.path.exists(r['path']):
            self._update_status(scan_note='render no longer on disk — rescan?')
            return
        try:
            plat.open_file(r['path'])
        except OSError as exc:
            self._update_status(scan_note=f'could not open: {exc}')

    def open_project(self):
        p = self._selected_project()
        if p is None:
            return
        if not os.path.exists(p['path']):
            self._update_status(scan_note='project file no longer exists — rescan?')
            return
        try:
            plat.open_file(p['path'])
        except OSError as exc:
            self._update_status(scan_note=f'could not open: {exc}')

    def reveal_selected(self):
        item = self._selected()
        if not item:
            return
        if item[0] == 'render':
            plat.reveal_file(item[1]['path'])
        elif item[0] == 'project':
            plat.reveal_file(item[1]['path'])

    def export_selected(self):
        project = self._selected_project()
        if project is None:
            messagebox.showinfo(APP_TITLE, 'Select a project (or one of its renders) first.')
            return
        stem = os.path.splitext(project['name'])[0]
        target = filedialog.asksaveasfilename(
            title='Export sample list', defaultextension='.txt',
            initialfile=f'{stem} — samples.txt',
            filetypes=[('Text file', '*.txt'), ('All files', '*.*')])
        if not target:
            return
        samples = project.get('samples', [])
        plugin = project.get('plugin_samples', [])
        missing = {m.lower() for m in self.index.missing_samples(samples + plugin)}
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(project['mtime']))
        lines = [f'Project : {project["name"]}', f'Path    : {project["path"]}',
                 f'Modified: {when}']
        if project.get('title'):
            lines.append(f'Title   : {project["title"]}')
        renders = project.get('renders', [])
        if renders:
            lines.append('')
            lines.append(f'Renders ({len(renders)}):')
            for r in renders:
                dur = _fmt_duration(r.get('duration')) or '?'
                lines.append(f'  {os.path.basename(r["path"])}  [{dur}]  {r["path"]}')
        lines.append('')
        lines.append(f'Samples ({len(samples)}):')
        for sname in samples:
            lines.append(f'  {"[MISSING] " if sname.lower() in missing else ""}{sname}')
        if plugin:
            lines.append('')
            lines.append(f'Plugin/embedded samples ({len(plugin)}):')
            for sname in plugin:
                lines.append(f'  {"[MISSING] " if sname.lower() in missing else ""}{sname}')
        lines.append('')
        lines.append(f'— exported by {APP_TITLE} ({CREDIT_URL})')
        try:
            with open(target, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            self._update_status(scan_note=f'exported to {os.path.basename(target)}')
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f'Could not write file:\n{exc}')

    # --------------------------------------------------------- context menu

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row or row not in self._item_map:
            return
        self.tree.selection_set(row)
        item = self._item_map[row]
        menu = self._context
        menu.delete(0, 'end')
        if item[0] == 'render':
            menu.add_command(label='Play', command=self.play_selected)
            menu.add_command(label='Reveal in file manager', command=self.reveal_selected)
            menu.add_separator()
            menu.add_command(label='Link to project…', command=self._link_dialog)
            menu.add_command(label='Not a render (hide)',
                             command=lambda: self._set_link(''))
            if item[1].get('path', '').lower() in \
                    (self.index.settings.get('manual_links') or {}):
                menu.add_command(label='Reset auto-match',
                                 command=lambda: self._set_link(None))
        elif item[0] == 'project':
            menu.add_command(label='Open in FL Studio', command=self.open_project)
            menu.add_command(label='Reveal in file manager', command=self.reveal_selected)
            menu.add_command(label='Export sample list…', command=self.export_selected)
        else:
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_link(self, target):
        r, _ = self._selected_render()
        if r is None:
            return
        self.index.set_manual_link(r['path'], target)
        self.refresh_results()

    def _link_dialog(self):
        r, _ = self._selected_render()
        if r is None:
            return
        c = self.theme
        dlg = tk.Toplevel(self)
        dlg.title('Link render to project')
        dlg.configure(bg=c['bg'])
        dlg.geometry('560x420')
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text=f'Link  {os.path.basename(r["path"])}  to which project?'
                  ).pack(anchor='w', padx=12, pady=(12, 6))
        entryvar = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=entryvar)
        ent.pack(fill='x', padx=12)
        ent.focus_set()
        frame = ttk.Frame(dlg)
        frame.pack(fill='both', expand=True, padx=12, pady=8)
        lb = tk.Listbox(frame, bg=c['panel'], fg=c['fg'], font=FONT,
                        selectbackground=c['select'], selectforeground=c['sel_fg'],
                        highlightthickness=0, borderwidth=0, activestyle='none')
        lb.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(frame, orient='vertical', command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')

        # _project_records() records key the filename off 'path' (they have no
        # 'name' field — only build() adds one), so derive it from the path.
        all_projects = sorted(self.index._project_records(),
                              key=lambda p: os.path.basename(p['path']).lower())
        shown = []

        def repopulate(*_):
            q = entryvar.get().lower()
            lb.delete(0, 'end')
            shown.clear()
            for p in all_projects:
                name = os.path.basename(p['path'])
                if q in name.lower() or q in p.get('title', '').lower():
                    shown.append(p)
                    lb.insert('end', os.path.splitext(name)[0])
        entryvar.trace_add('write', repopulate)
        repopulate()

        def confirm(*_):
            sel = lb.curselection()
            if sel:
                self.index.set_manual_link(r['path'], shown[sel[0]]['path'])
                dlg.destroy()
                self.refresh_results()
        lb.bind('<Double-1>', confirm)
        btns = ttk.Frame(dlg)
        btns.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(btns, text='Link', command=confirm).pack(side='right')
        ttk.Button(btns, text='Cancel', command=dlg.destroy).pack(side='right', padx=8)

    # ----------------------------------------------------------- settings

    def _on_backups_toggled(self):
        self.index.settings['include_backups'] = self.backups_var.get()
        self.index.invalidate()
        self.index.save()
        self.start_scan()

    def _on_long_toggled(self):
        self.index.settings['include_long_unmatched'] = self.long_var.get()
        self.index.invalidate()
        self.index.save()
        # A rescan is needed because this changes which files get probed.
        self.start_scan()

    def open_folders_dialog(self):
        c = self.theme
        dlg = tk.Toplevel(self)
        dlg.title('Scanned folders')
        dlg.configure(bg=c['bg'])
        dlg.geometry('720x460')
        dlg.transient(self)
        dlg.grab_set()

        def make_list(parent, label, key):
            ttk.Label(parent, text=label).pack(anchor='w', padx=12, pady=(10, 4))
            frame = ttk.Frame(parent)
            frame.pack(fill='both', expand=True, padx=12)
            lb = tk.Listbox(frame, bg=c['panel'], fg=c['fg'], font=FONT,
                            selectbackground=c['select'], selectforeground=c['sel_fg'],
                            highlightthickness=0, borderwidth=0, activestyle='none',
                            height=5)
            lb.pack(side='left', fill='both', expand=True)
            sb = ttk.Scrollbar(frame, orient='vertical', command=lb.yview)
            lb.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
            for folder in self.index.settings.get(key, []):
                lb.insert('end', folder)
            btns = ttk.Frame(parent)
            btns.pack(fill='x', padx=12, pady=(4, 2))

            def add():
                folder = filedialog.askdirectory(parent=dlg, title='Add a folder')
                if folder:
                    folder = os.path.normpath(folder)
                    if folder.lower() not in [f.lower() for f in lb.get(0, 'end')]:
                        lb.insert('end', folder)

            def remove():
                for i in reversed(lb.curselection()):
                    lb.delete(i)
            ttk.Button(btns, text='Add…', command=add).pack(side='left')
            ttk.Button(btns, text='Remove', command=remove).pack(side='left', padx=8)
            return lb

        proj_lb = make_list(dlg, 'Folders scanned for FL Studio projects (.flp):',
                            'project_folders')
        audio_lb = make_list(dlg, 'Folders scanned for rendered audio '
                            '(add your export / sample folders):', 'audio_folders')

        actions = ttk.Frame(dlg)
        actions.pack(fill='x', padx=12, pady=12)

        def close():
            new_proj = list(proj_lb.get(0, 'end'))
            new_audio = list(audio_lb.get(0, 'end'))
            changed = (new_proj != self.index.settings.get('project_folders')
                       or new_audio != self.index.settings.get('audio_folders'))
            self.index.settings['project_folders'] = new_proj
            self.index.settings['audio_folders'] = new_audio
            self.index.invalidate()
            self.index.save()
            dlg.destroy()
            if changed:
                self.refresh_results()
                self.start_scan()
        ttk.Button(actions, text='Done', command=close).pack(side='right')
        dlg.protocol('WM_DELETE_WINDOW', close)

    # ------------------------------------------------------------ scanning

    def start_scan(self):
        if self._scanning:
            self._scan_pending = True
            return
        self._scanning = True
        self.rescan_btn.state(['disabled'])
        self.progress.configure(value=0, maximum=1)
        self.progress.pack(side='right')
        self.status_var.set('Scanning…')

        def worker():
            try:
                stats = self.index.scan(
                    progress_cb=lambda ph, d, t: self._scan_queue.put(('prog', ph, d, t)))
                self._scan_queue.put(('done', stats))
            except Exception as exc:
                self._scan_queue.put(('fail', repr(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(80, self._poll_scan)

    def _poll_scan(self):
        finished = None
        try:
            while True:
                msg = self._scan_queue.get_nowait()
                if msg[0] == 'prog':
                    _, phase, done, total = msg
                    self.progress.configure(maximum=max(total, 1), value=done)
                    label = 'projects' if phase == 'projects' else 'renders'
                    self.status_var.set(f'Scanning {label}…  {done}/{total}')
                else:
                    finished = msg
        except queue.Empty:
            pass
        if finished is None:
            self.after(80, self._poll_scan)
            return
        self._scanning = False
        self.rescan_btn.state(['!disabled'])
        self.progress.pack_forget()
        if finished[0] == 'fail':
            self.status_var.set(f'Scan failed: {finished[1]}')
        else:
            stats = finished[1]
            self.index.clear_exists_cache()
            note = (f"scanned in {stats['seconds']}s "
                    f"({stats['probed_audio']} audio probed)")
            self.refresh_results()
            self._update_status(scan_note=note)
        if self._scan_pending:
            self._scan_pending = False
            self.start_scan()

    # -------------------------------------------------------------- about

    def show_about(self):
        dlg = tk.Toplevel(self)
        dlg.title(f'About {APP_TITLE}')
        dlg.configure(bg=self.theme['bg'])
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        try:
            dlg.iconphoto(False, self._icon_img)
        except Exception:
            pass
        pad = ttk.Frame(dlg, padding=20)
        pad.pack(fill='both', expand=True)
        try:
            small = self._icon_img.subsample(max(self._icon_img.width() // 96, 1))
            lbl = ttk.Label(pad, image=small)
            lbl.image = small
            lbl.pack(pady=(0, 10))
        except Exception:
            pass
        ttk.Label(pad, text=APP_TITLE, font=(FONT[0], 16, 'bold')).pack()
        ttk.Label(pad, text='Find every render FL Studio exported, treed by project.',
                  style='Dim.TLabel').pack(pady=(4, 12))
        link = ttk.Label(pad, text='Developed by ajh — ajh.wtf',
                         style='Credit.TLabel', cursor='hand2')
        link.pack()
        link.bind('<Button-1>', lambda e: webbrowser.open(CREDIT_URL))
        ttk.Button(pad, text='Close', command=dlg.destroy).pack(pady=(16, 0))


def main():
    App().mainloop()


if __name__ == '__main__':
    main()
