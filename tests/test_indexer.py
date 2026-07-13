import os
import tempfile
import unittest

import _fixtures as fx
from indexer import Index


class TestIndexerEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.proj = os.path.join(self.root, 'Projects')
        for d in ['Projects', 'Projects/My Beat', 'Projects/Rendered',
                  'Samples/Drums', 'Exports']:
            os.makedirs(os.path.join(self.root, d), exist_ok=True)

        fx.make_flp(os.path.join(self.proj, 'My Beat.flp'), title='My Beat',
                    samples=[r'C:\Samples\Drums\kick.wav', r'C:\Samples\Drums\hat.wav'])
        fx.make_flp(os.path.join(self.proj, 'Untitled.flp'), title='')
        fx.make_wav(os.path.join(self.proj, 'My Beat.wav'), 180)
        fx.make_wav(os.path.join(self.proj, 'My Beat', 'Master.wav'), 181)
        fx.make_wav(os.path.join(self.proj, 'Rendered', 'My Beat master 140 Fmin.wav'), 182)
        fx.make_wav(os.path.join(self.root, 'Exports', 'old track.wav'), 200,
                    extra_chunks=fx.acid_chunk(174))
        # samples that must NOT be probed or treated as renders
        fx.make_wav(os.path.join(self.root, 'Samples', 'Drums', 'kick.wav'), 0.4)
        fx.make_wav(os.path.join(self.root, 'Samples', 'Drums', 'hat.wav'), 0.3)
        fx.make_wav(os.path.join(self.root, 'Samples', 'Drums', 'ambient loop.wav'), 70)

        self.idx = Index(cache_file=os.path.join(self.root, 'index.json'))
        self.idx.settings['project_folders'] = [self.proj]
        self.idx.settings['audio_folders'] = [self.root]

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_and_match(self):
        stats = self.idx.scan(progress_cb=lambda *a: None)
        self.assertEqual(stats['projects'], 2)
        # only renders/render-folder files probed, not the 3 samples
        self.assertEqual(stats['probed_audio'], 4)

        projects, orphans = self.idx.search('', only_with_renders=False)
        by_name = {p['name']: p for p in projects}
        self.assertEqual(by_name['My Beat.flp']['render_count'], 3)
        render_names = {os.path.basename(r['path'])
                        for r in by_name['My Beat.flp']['renders']}
        self.assertEqual(render_names,
                         {'My Beat.wav', 'Master.wav', 'My Beat master 140 Fmin.wav'})
        self.assertEqual(by_name['Untitled.flp']['render_count'], 0)
        self.assertTrue(any('old track.wav' in r['path'] for r in orphans))
        # the long ambient loop must not appear anywhere as a render/orphan
        self.assertFalse(any('ambient loop' in r['path'] for r in orphans))

    def test_incremental_rescan_is_cheap(self):
        self.idx.scan(progress_cb=lambda *a: None)
        stats2 = self.idx.scan(progress_cb=lambda *a: None)  # nothing changed
        self.assertEqual(stats2['parsed_projects'], 0)
        self.assertEqual(stats2['probed_audio'], 0)

    def test_search_filters(self):
        self.idx.scan(progress_cb=lambda *a: None)
        projects, _ = self.idx.search('beat', only_with_renders=True)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]['name'], 'My Beat.flp')
        # searching a sample name finds the project that used it
        projects2, _ = self.idx.search('kick', only_with_renders=True)
        self.assertTrue(any(p['name'] == 'My Beat.flp' for p in projects2))

    def test_manual_link(self):
        self.idx.scan(progress_cb=lambda *a: None)
        orphan_path = os.path.join(self.root, 'Exports', 'old track.wav')
        self.idx.set_manual_link(orphan_path, os.path.join(self.proj, 'Untitled.flp'))
        projects, orphans = self.idx.search('', only_with_renders=False)
        by_name = {p['name']: p for p in projects}
        self.assertEqual(by_name['Untitled.flp']['render_count'], 1)
        self.assertFalse(any('old track.wav' in r['path'] for r in orphans))

    def test_manual_link_to_missing_project_kept_as_orphan(self):
        # Regression: linking to a project not in scope used to silently drop
        # the render from every view.
        self.idx.scan(progress_cb=lambda *a: None)
        orphan_path = os.path.join(self.root, 'Exports', 'old track.wav')
        self.idx.set_manual_link(orphan_path, r'C:\Nowhere\Ghost.flp')
        _, orphans = self.idx.search('', only_with_renders=False)
        self.assertTrue(any('old track.wav' in r['path'] for r in orphans))

    def test_manual_hide(self):
        self.idx.scan(progress_cb=lambda *a: None)
        orphan_path = os.path.join(self.root, 'Exports', 'old track.wav')
        self.idx.set_manual_link(orphan_path, '')  # hide
        _, orphans = self.idx.search('', only_with_renders=False)
        self.assertFalse(any('old track.wav' in r['path'] for r in orphans))

    def test_cache_persists(self):
        self.idx.scan(progress_cb=lambda *a: None)
        idx2 = Index(cache_file=os.path.join(self.root, 'index.json'))
        stats = idx2.stats()
        self.assertEqual(stats['projects'], 2)
        self.assertEqual(stats['audio_indexed'], 4)


if __name__ == '__main__':
    unittest.main()
