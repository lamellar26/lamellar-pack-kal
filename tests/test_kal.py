"""kal pack tests. Run: python3 -m unittest discover -s tests  (from the pack dir; lamellar core must be importable)."""
import contextlib, io, os, shutil, sys, tempfile, unittest, zipfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
for p in (os.environ.get('LAMELLAR_CORE', ''), os.path.join(os.path.dirname(HERE), 'lamellar')):
    if p and os.path.isdir(p): sys.path.insert(0, p)
import pack
from kalcore import store

DOC1 = "# 羅馬書 第3章\n\n3:23 因為世人都犯了罪，虧缺了神的榮耀；\n3:24 如今卻蒙神的恩典，因基督耶穌的救贖，就白白地稱義。\n"
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
        docx(os.path.join(self.lib, 'policy.docx'), ['Leave requests go to Mandy in HR.', '請假要提早三日通知。'])
        self.cfg = {'model': 'm', 'context': 8192, 'state_dir': self.t, 'max_output_tokens': 100, 'endpoint': 'http://127.0.0.1:9/v1'}
        self.c = store.connect(store.lib_path(self.cfg)); store.add_folder(self.c, self.lib)

    def tearDown(self): self.c.close(); shutil.rmtree(self.t)


class KL1Index(Base):
    def test_search_now_two_char_and_docx(self):
        self.assertEqual(store.search(self.c, '稱義')[0]['ref'], 'docs/rom03.md#1')
        self.assertTrue(store.search(self.c, 'Mandy HR leave')[0]['ref'].startswith('docs/policy.docx'))
        self.assertTrue(store.search(self.c, '請假通知'))

    def test_incremental_no_duplicates_and_change(self):
        st = store.add_folder(self.c, self.lib); self.assertEqual((st['added'], st['changed'], st['chunks']), (0, 0, 0))
        n = store.status(self.c)['chunks']
        with open(os.path.join(self.lib, 'notes.txt'), 'a') as f: f.write('\n\nThe fridge code is 4471.')
        st = store.add_folder(self.c, self.lib); self.assertEqual(st['changed'], 1)
        self.assertEqual(store.status(self.c)['chunks'], n)                 # old chunks inactive, not duplicated in results
        self.assertEqual(len(store.search(self.c, 'oven unit Friday')), 1)

    def test_split_caps(self):
        parts = store.split('句子。' * 2000)
        self.assertTrue(all(len(p) <= store.HARD for p in parts)); self.assertEqual(''.join(parts), '句子。' * 2000)


class KL2Context(Base):
    def test_irrelevant_gets_nothing(self):
        for q in ['點樣整蛋撻好食', 'write a python function to parse csv', '今日天氣點']:
            self.assertEqual(pack.context(self.cfg, q), '', q)

    def test_relevant_has_ref_and_cap(self):
        s = pack.context(self.cfg, '世人都犯了罪，點樣稱義？')
        self.assertIn('[docs/rom03.md#1]', s); self.assertLessEqual(len(s), 3000)
        big = os.path.join(self.t, 'big'); os.makedirs(big)
        for i in range(20):
            with open(os.path.join(big, f'b{i}.md'), 'w') as f: f.write(('稱義的道理第%d段。' % i) * 80)
        store.add_folder(self.c, big)
        self.assertLessEqual(len(pack.context(self.cfg, '稱義的道理')), 3000)

    def test_tools(self):
        ok, out = pack.kal_search({'cfg': self.cfg}, '稱義'); self.assertTrue(ok); self.assertIn('docs/rom03.md#1', out)
        ok, out = pack.kal_read({'cfg': self.cfg}, '[docs/rom03.md#1]'); self.assertTrue(ok); self.assertIn('白白地稱義', out)
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
