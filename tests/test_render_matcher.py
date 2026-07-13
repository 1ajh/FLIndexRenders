import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import render_matcher as rm


class TestNormalization(unittest.TestCase):
    def test_strict_strips_copy_and_paren(self):
        self.assertEqual(rm.normalize_strict('My Beat (2)'), 'my beat')
        self.assertEqual(rm.normalize_strict('My_Beat - Copy'), 'my beat')
        self.assertEqual(rm.normalize_strict('My-Beat'), 'my beat')

    def test_loose_strips_decorations(self):
        self.assertEqual(rm.normalize_loose('My Beat master 140 Fmin'), 'my beat')
        self.assertEqual(rm.normalize_loose('01 My Beat mixdown'), 'my beat')
        self.assertEqual(rm.normalize_loose('My Beat v3'), 'my beat')

    def test_loose_keeps_title_words(self):
        # 'beat'/'loop'/'flip'/'final' are common in real titles: don't eat them.
        self.assertEqual(rm.normalize_loose('My Beat'), 'my beat')
        self.assertEqual(rm.normalize_loose('Final Boss'), 'final boss')
        self.assertEqual(rm.normalize_loose('Trap Loop'), 'trap loop')

    def test_loose_leading_keyword_not_stripped(self):
        # Leading strip is numeric-only: 'Version'/'Take' as first words stay,
        # but a leading track number goes and a trailing take/version is removed.
        self.assertEqual(rm.normalize_loose('Version Control'), 'version control')
        self.assertEqual(rm.normalize_loose('Take Five'), 'take five')
        self.assertEqual(rm.normalize_loose('01 My Beat'), 'my beat')
        self.assertEqual(rm.normalize_loose('My Beat take 2'), 'my beat')

    def test_unicode_fold(self):
        self.assertEqual(rm.normalize_strict('Café'), rm.normalize_strict('café'))
        # emoji folded away
        self.assertEqual(rm.normalize_strict('fire beat 🔥'), 'fire beat')

    def test_generic_detection(self):
        self.assertTrue(rm.is_generic('untitled', 'untitled'))
        self.assertTrue(rm.is_generic('beat', 'beat'))
        self.assertFalse(rm.is_generic('my beat', 'my beat'))


def audio(path, **kw):
    return rm.make_audio_record(path, **kw)


def project(path, **kw):
    return rm.make_project_record(path, **kw)


class TestMatching(unittest.TestCase):
    def test_exact_name_same_folder(self):
        p = project(r'C:\FL\My Beat.flp')
        a = audio(r'C:\FL\My Beat.wav', duration=180)
        score, reason = rm.score_pair(a, p)
        self.assertGreaterEqual(score, rm.MATCH_THRESHOLD)
        self.assertEqual(rm.confidence_of(score), 'high')

    def test_loose_match_render_subfolder(self):
        p = project(r'C:\FL\My Beat.flp')
        a = audio(r'C:\FL\Rendered\My Beat master 140 Fmin.mp3', duration=182)
        score, _ = rm.score_pair(a, p)
        self.assertGreaterEqual(score, rm.MATCH_THRESHOLD)

    def test_stem_folder_named_after_project(self):
        p = project(r'C:\FL\My Beat.flp')
        a = audio(r'C:\FL\My Beat\Master.wav', duration=181)
        score, reason = rm.score_pair(a, p)
        self.assertGreaterEqual(score, rm.MATCH_THRESHOLD)
        self.assertIn('folder', reason)

    def test_input_sample_is_not_a_render(self):
        # A file the project uses as an input sample can't be its render.
        p = project(r'C:\FL\Kick.flp', samples=[r'C:\Samples\kick.wav'])
        a = audio(r'C:\Samples\kick.wav', duration=0.5, used_as_sample=True)
        score, _ = rm.score_pair(a, p)
        self.assertEqual(score, 0)

    def test_reimported_render_still_matches_own_project(self):
        # kick.wav is a render of project A but also imported into B as a sample.
        # It must still match A (per-project, not global, exclusion).
        a_proj = project(r'C:\FL\kick.flp')
        a = audio(r'C:\FL\kick.wav', duration=190, used_as_sample=True)
        score, _ = rm.score_pair(a, a_proj)
        self.assertGreaterEqual(score, rm.MATCH_THRESHOLD)

    def test_generic_needs_proximity(self):
        p = project(r'C:\FL\Untitled.flp')
        near = audio(r'C:\FL\Untitled.wav', duration=180)
        far = audio(r'D:\Random\Untitled.wav', duration=180)
        self.assertGreaterEqual(rm.score_pair(near, p)[0], rm.MATCH_THRESHOLD)
        self.assertEqual(rm.score_pair(far, p)[0], 0)

    def test_short_duration_penalized(self):
        p = project(r'C:\FL\My Beat.flp')
        a = audio(r'C:\FL\My Beat.wav', duration=0.5)  # a 0.5s "render" is doubtful
        long = audio(r'C:\FL\My Beat.wav', duration=180)
        self.assertLess(rm.score_pair(a, p)[0], rm.score_pair(long, p)[0])

    def test_prefix_does_not_overmatch_short_roots(self):
        p = project(r'C:\FL\beat.flp')  # generic + short
        a = audio(r'C:\FL\beatmania_full_mix.wav', duration=200)
        self.assertEqual(rm.score_pair(a, p)[0], 0)


class TestClassify(unittest.TestCase):
    def test_full_classify(self):
        projects = [project(r'C:\FL\My Beat.flp', samples=[r'C:\S\kick.wav']),
                    project(r'C:\FL\Untitled.flp')]
        audios = [
            audio(r'C:\FL\My Beat.wav', duration=180),
            audio(r'C:\FL\My Beat\Master.wav', duration=181),
            audio(r'C:\FL\Rendered\My Beat master.mp3', duration=182),
            audio(r'C:\S\kick.wav', duration=0.4, used_as_sample=True),
            audio(r'C:\Music\Exports\old track.wav', duration=200),
        ]
        matches, orphans = rm.classify(projects, audios)
        mybeat = matches[r'C:\FL\My Beat.flp']
        names = {os.path.basename(r['path']) for r in mybeat}
        self.assertEqual(names, {'My Beat.wav', 'Master.wav', 'My Beat master.mp3'})
        # kick.wav excluded (input sample); old track.wav is an orphan (render folder)
        self.assertTrue(any('old track.wav' in r['path'] for r in orphans))
        self.assertFalse(any('kick.wav' in r['path'] for r in orphans))

    def test_long_sample_not_flooding_orphans(self):
        projects = [project(r'C:\FL\My Beat.flp')]
        audios = [audio(r'C:\Samples\ambient loop.wav', duration=90)]  # long, not render folder
        _, orphans = rm.classify(projects, audios)
        self.assertEqual(orphans, [])

    def test_include_long_surfaces_them(self):
        projects = [project(r'C:\FL\My Beat.flp')]
        audios = [audio(r'C:\Samples\ambient loop.wav', duration=90)]
        _, orphans = rm.classify(projects, audios, include_long_unmatched=True)
        self.assertEqual(len(orphans), 1)

    def test_accent_folded_match_via_classify(self):
        # Regression: classify() didn't key on key_ascii, so accent-only name
        # differences were never even candidates.
        projects = [project(r'C:\FL\Cafe.flp')]
        audios = [audio(r'C:\FL\Café.wav', duration=180)]
        matches, _ = rm.classify(projects, audios)
        self.assertEqual(len(matches[r'C:\FL\Cafe.flp']), 1)

    def test_variant_dedup_count(self):
        recs = [audio(r'C:\FL\My Beat.wav', duration=180),
                audio(r'C:\FL\My Beat.mp3', duration=180),
                audio(r'C:\FL\My Beat 24bit.wav', duration=180)]
        self.assertEqual(rm.count_unique_renders(recs), 1)


if __name__ == '__main__':
    unittest.main()
