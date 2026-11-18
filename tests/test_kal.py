"""kal pack tests. Run: python3 -m unittest discover -s tests  (from the pack dir; lamellar core must be importable)."""
import contextlib, io, os, shutil, sys, tempfile, unittest, zipfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
for p in (os.environ.get('LAMELLAR_CORE', ''), os.path.join(os.path.dirname(HERE), 'lamellar')):
    if p and os.path.isdir(p): sys.path.insert(0, p)
import json
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures.json'), encoding='utf-8') as f: T = json.load(f)   # Chinese test text
import pack
from kalcore import store

DOC1 = T['kal_module_1']
DOC2 = "Kitchen notes\n\nThe oven in unit 7 must be cleaned every Friday by the night shift.\n\nInvoices above 5000 dollars need two signatures."


def docx(path, paras):
    body = ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paras)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('word/document.xml', f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(); self.lib = os.path.join(self.t, 'docs'); os.makedirs(self.lib)
        with open(os.path.join(self.lib, 'rom03.md'), 'w') as f: f.write(DOC1)
        with open(os.path.join(self.lib, 'notes.txt'), 'w') as f: f.write(DOC2)
        docx(os.path.join(self.lib, 'policy.docx'), ['Leave requests go to Mandy in HR.', T['kal_setUp_1']])
        self.cfg = {'model': 'm', 'context': 8192, 'state_dir': self.t, 'max_output_tokens': 100, 'endpoint': 'http://127.0.0.1:9/v1'}
        self.c = store.connect(store.lib_path(self.cfg)); store.add_folder(self.c, self.lib)

    def tearDown(self): self.c.close(); shutil.rmtree(self.t)


class KL1Index(Base):
    def test_search_now_two_char_and_docx(self):
        self.assertEqual(store.search(self.c, T['kal_search_now_two_char_and_docx_1'])[0]['ref'], 'docs/rom03.md#1')
        self.assertTrue(store.search(self.c, 'Mandy HR leave')[0]['ref'].startswith('docs/policy.docx'))
        self.assertTrue(store.search(self.c, T['kal_search_now_two_char_and_docx_2']))

    def test_incremental_no_duplicates_and_change(self):
        st = store.add_folder(self.c, self.lib); self.assertEqual((st['added'], st['changed'], st['chunks']), (0, 0, 0))
        n = store.status(self.c)['chunks']
        with open(os.path.join(self.lib, 'notes.txt'), 'a') as f: f.write('\n\nThe fridge code is 4471.')
        st = store.add_folder(self.c, self.lib); self.assertEqual(st['changed'], 1)
        self.assertEqual(store.status(self.c)['chunks'], n)                 # old chunks inactive, not duplicated in results
        self.assertEqual(len(store.search(self.c, 'oven unit Friday')), 1)

    def test_split_caps(self):
        parts = store.split(T['kal_split_caps_1'] * 2000)
        self.assertTrue(all(len(p) <= store.HARD for p in parts)); self.assertEqual(''.join(parts), T['kal_split_caps_2'] * 2000)


class KL2Context(Base):
    def test_irrelevant_gets_nothing(self):
        for q in [T['kal_irrelevant_gets_nothing_1'], 'write a python function to parse csv', T['kal_irrelevant_gets_nothing_2']]:
            self.assertEqual(pack.context(self.cfg, q), '', q)

    def test_relevant_has_ref_and_cap(self):
        s = pack.context(self.cfg, T['kal_relevant_has_ref_and_cap_1'])
        self.assertIn('[docs/rom03.md#1]', s); self.assertLessEqual(len(s), 3000)
        big = os.path.join(self.t, 'big'); os.makedirs(big)
        for i in range(20):
            with open(os.path.join(big, f'b{i}.md'), 'w') as f: f.write((T['kal_relevant_has_ref_and_cap_2'] % i) * 80)
        store.add_folder(self.c, big)
        self.assertLessEqual(len(pack.context(self.cfg, T['kal_relevant_has_ref_and_cap_3'])), 3000)

    def test_tools(self):
        ok, out = pack.kal_search({'cfg': self.cfg}, T['kal_tools_1']); self.assertTrue(ok); self.assertIn('docs/rom03.md#1', out)
        ok, out = pack.kal_read({'cfg': self.cfg}, '[docs/rom03.md#1]'); self.assertTrue(ok); self.assertIn(T['kal_tools_2'], out)
        self.assertFalse(pack.kal_read({'cfg': self.cfg}, 'nope#1')[0])

    def test_registered_through_core(self):
        from lamellar.core import packs, tools
        reg = packs.load({**self.cfg, 'packs': ['kal'], 'packs_dir': [os.path.dirname(HERE)]})
        self.assertFalse(reg.errors); self.assertIn('kal', reg.commands); self.assertIn('kal_search', tools.NAMES); self.assertIn('kal_read', tools.NAMES)


class KL1Status(Base):
    def test_status_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): pack.command(['status'], self.cfg)
        self.assertIn('3 files', out.getvalue()); self.assertIn('passages', out.getvalue())


if __name__ == '__main__':
    unittest.main()
