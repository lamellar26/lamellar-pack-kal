"""kal pack tests. Run: python3 -m unittest discover -s tests  (from the pack dir; lamellar core must be importable)."""
import contextlib, io, json, os, shutil, sys, tempfile, time, unittest, zipfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
for p in (os.environ.get('LAMELLAR_CORE', ''), os.path.join(os.path.dirname(HERE), 'lamellar')):
    if p and os.path.isdir(p): sys.path.insert(0, p)
import pack
from kalcore import store, digest

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

    def wid(self, name):
        return self.c.execute("SELECT w.id FROM window w JOIN source s ON s.id=w.source_id WHERE s.rel LIKE ?", ('%' + name + '%',)).fetchone()[0]


class KL1Index(Base):
    def test_search_now_two_char_and_docx(self):
        r = store.search(self.c, '稱義')
        self.assertEqual(r['chunks'][0]['ref'], 'docs/rom03.md#1')
        self.assertTrue(store.search(self.c, 'Mandy HR leave')['chunks'][0]['ref'].startswith('docs/policy.docx'))
        self.assertTrue(store.search(self.c, '請假通知')['chunks'])

    def test_incremental_no_duplicates_and_change(self):
        st = store.add_folder(self.c, self.lib); self.assertEqual((st['added'], st['changed'], st['chunks']), (0, 0, 0))
        n = store.status(self.c)['chunks']
        with open(os.path.join(self.lib, 'notes.txt'), 'a') as f: f.write('\n\nThe fridge code is 4471.')
        st = store.add_folder(self.c, self.lib); self.assertEqual(st['changed'], 1)
        self.assertEqual(store.status(self.c)['chunks'], n)                 # old chunks inactive, not duplicated in results
        self.assertEqual(len(store.search(self.c, 'oven unit Friday')['chunks']), 1)

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

    def test_nodes_first_and_bring_their_refs(self):
        store.add_node(self.c, {'title': '3:24 白白稱義', 'body': '蒙神的恩典白白稱義', 'quotes': ['就白白地稱義'], 'refs': ['docs/rom03.md#1'], 'terms': ['稱義']}); self.c.commit()
        s = pack.context(self.cfg, '白白稱義係乜意思')
        self.assertLess(s.index('3:24 白白稱義'), s.index('[docs/rom03.md#1]'))
        self.assertNotIn('3:24 白白稱義', pack.context(self.cfg, '白白稱義係乜意思', use_nodes=False))

    def test_tools(self):
        ok, out = pack.kal_search({'cfg': self.cfg}, '稱義'); self.assertTrue(ok); self.assertIn('docs/rom03.md#1', out)
        ok, out = pack.kal_read({'cfg': self.cfg}, '[docs/rom03.md#1]'); self.assertTrue(ok); self.assertIn('白白地稱義', out)
        self.assertFalse(pack.kal_read({'cfg': self.cfg}, 'nope#1')[0])

    def test_registered_through_core(self):
        from lamellar.core import packs, tools
        reg = packs.load({**self.cfg, 'packs': ['kal'], 'packs_dir': [os.path.dirname(HERE)]})
        self.assertFalse(reg.errors); self.assertIn('kal', reg.commands); self.assertIn('kal_search', tools.NAMES); self.assertIn('kal_read', tools.NAMES)


GOOD = {'title': '3:23 世人都犯了罪', 'body': '世人都犯了罪，虧缺了神的榮耀。', 'quotes': ['因為世人都犯了罪，虧缺了神的榮耀'], 'refs': ['docs/rom03.md#1'], 'terms': ['犯了罪', '榮耀']}


class KL3QC(Base):
    def texts(self): return digest.window_texts(self.c, self.wid('rom03'))

    def test_good_passes(self): self.assertIsNone(digest.qc(GOOD, self.texts()))

    def test_rules(self):
        t = self.texts()
        cases = {'no refs': {**GOOD, 'refs': []}, 'refs not in': {**GOOD, 'refs': ['docs/other.md#1']},
                 'no quotes': {**GOOD, 'quotes': []}, 'not found word for word': {**GOOD, 'quotes': ['世人全部都犯罪']},
                 'term not in': {**GOOD, 'terms': ['因信稱義']}, "number '5:1'": {**GOOD, 'title': '5:1 世人都犯了罪'},
                 'missing body': {**GOOD, 'body': ''}, 'same language': {**GOOD, 'body': 'All have sinned and fall short of the glory of God.'}, 'too short': {**GOOD, 'quotes': ['世人']}}
        for want, node in cases.items():
            why = digest.qc(node, t); self.assertIsNotNone(why, want); self.assertIn(want.split("'")[0].strip(), why)

    def test_bracketed_refs_accepted(self):
        n = {**GOOD, 'refs': ['[docs/rom03.md#1]']}
        self.assertIsNone(digest.qc(n, self.texts())); self.assertEqual(n['refs'], ['docs/rom03.md#1'])

    def test_quote_tolerates_spaces_and_trailing_punct(self):
        self.assertIsNone(digest.qc({**GOOD, 'quotes': ['如今卻蒙神的恩典， 因基督耶穌的救贖，就白白地稱義。']}, self.texts()))

    def test_parse_json_wrapped(self):
        self.assertEqual(digest.parse_json('ok ```json\n{"nodes": [{"a": "}"}]}\n``` done')['nodes'][0]['a'], '}')
        self.assertIsNone(digest.parse_json('no json here'))


class KL3Learn(Base):
    def fake(self, bad_first=False):
        calls = {'n': 0}
        def chat(msgs):
            calls['n'] += 1; txt = msgs[0]['content']
            if 'rom03.md' in txt:
                if bad_first and len(msgs) == 1: return json.dumps({'nodes': [GOOD, {**GOOD, 'title': 'x', 'quotes': ['亂作的句子在此']}]})
                if bad_first: return json.dumps({'nodes': [{**GOOD, 'title': '3:24 白白稱義', 'quotes': ['就白白地稱義'], 'terms': ['稱義']}]})
                return json.dumps({'nodes': [GOOD]})
            if 'notes.txt' in txt: return 'not json at all'
            return json.dumps({'nodes': [{'title': 'Leave', 'body': 'Leave requests go to Mandy.', 'quotes': ['Leave requests go to Mandy in HR.'],
                                          'refs': list(digest.window_texts(self.c, self.wid('policy'))), 'terms': ['Mandy']}]})
        return chat, calls

    def test_bounce_then_fixed(self):
        chat, calls = self.fake(bad_first=True)
        good, rej, note = digest.digest_window(self.c, self.wid('rom03'), chat)
        self.assertEqual((len(good), rej), (2, 0)); self.assertEqual(calls['n'], 2)

    def test_gives_up_after_two_bounces(self):
        def chat(msgs): return json.dumps({'nodes': [{**GOOD, 'quotes': ['亂作的句子在此']}]})
        good, rej, _ = digest.digest_window(self.c, self.wid('rom03'), chat); self.assertEqual((good, rej), ([], 1))

    def test_resume_deadline_busy_and_status(self):
        chat, _ = self.fake()
        st = digest.learn(self.c, chat, deadline=time.time() - 1, log=lambda m: None)   # deadline already passed: nothing done
        self.assertEqual((st['done'], st['stopped']), (0, 'deadline'))
        st = digest.learn(self.c, chat, busy=lambda: True, log=lambda m: None)            # user busy: windows stay pending
        self.assertEqual(st['done'], 0); self.assertEqual(store.status(self.c)['windows_pending'], 3)
        n = {'k': 0}
        def busy_after_one(): n['k'] += 1; return n['k'] > 2
        st = digest.learn(self.c, chat, busy=busy_after_one, log=lambda m: None)
        self.assertEqual(st['done'] + st['failed'], 1); self.assertEqual(st['stopped'], 'user job running')
        st = digest.learn(self.c, chat, log=lambda m: None)                               # resume: only the rest
        s = store.status(self.c)
        self.assertEqual(s['windows_pending'], 0); self.assertEqual(s['windows_done'] + s['windows_failed'], 3)
        self.assertEqual(s['windows_failed'], 1)                                          # the not-json window
        self.assertGreaterEqual(s['nodes'], 2)
        for _ in range(5): digest.learn(self.c, chat, log=lambda m: None)                 # failed windows retried at most max_tries
        self.assertEqual(self.c.execute("SELECT tries FROM window WHERE status='failed'").fetchone()[0], 3)

    def test_status_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): pack.command(['status'], self.cfg)
        self.assertIn('3 files', out.getvalue()); self.assertIn('pending', out.getvalue())


if __name__ == '__main__':
    unittest.main()
