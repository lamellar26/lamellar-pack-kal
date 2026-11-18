"""KL6 gates: conversation -> the user's private KAL library (original text only). Run from the pack dir: python3 -m unittest discover -s tests"""
import os, shutil, sys, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
for p in (os.environ.get('LAMELLAR_CORE', ''), os.path.join(os.path.dirname(HERE), 'lamellar')):
    if p and os.path.isdir(p): sys.path.insert(0, p)
import pack
from kalcore import store, chat as kc

TOOL_OUT = ("報價單 Q-2031：客戶 Bright Harbour 訂 40 部 LeArm 機械臂，交貨期 2026-12-05。\n"
            "倉存表顯示 LeArm 現貨 25 部，另外 15 部要向深圳廠補貨。")
TASK = '幫我睇吓 quotes/Q-2031.txt 份報價單，交唔交得切貨？'
ANSWER = '份報價單要 40 部，現貨得 25 部，所以要補 15 部。我建議即日落單補貨，應該趕得切 12 月 5 號。'
Q = 'Bright Harbour 訂咗幾多部 LeArm？'


def msgs_for(task, tool_out):
    return [{'role': 'user', 'content': task + '\n\n[kal] ...'},
            {'role': 'assistant', 'content': ANSWER, 'tool_calls': [{'id': 'c1'}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': tool_out}]


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.cfg = {'model': 'm', 'context': 8192, 'state_dir': self.t, 'max_output_tokens': 100, 'endpoint': 'http://127.0.0.1:9/v1',
                    'armor_bot': 'web', 'armor_chat': 'alice', 'kal': {'chat_write': True, 'chat_rerank': False}}

    def tearDown(self): shutil.rmtree(self.t)

    def learn(self, task=TASK, tool=TOOL_OUT, cfg=None):
        msgs = msgs_for(task, tool) if tool else [{'role': 'user', 'content': task}]
        return kc.learn_exchange(cfg or self.cfg, task, msgs, 1 if tool else 0, 'sess1')

    def db(self, cfg=None): return store.connect(store.lib_path(cfg or self.cfg, kc.lib_name(cfg or self.cfg)))


class KL6Trigger(Base):
    PURE = ['你是誰？', 'hi', '你有記憶嗎？', 'The North Face的產品耐用嗎？', '多謝', '你能寫程式嗎？', '在嗎？', '你是什麼model?', 'tnf t shirt耐用嗎？', 'OK']
    FACTS = ['我哋公司用 Xero 做帳，月結喺每月 5 號。', 'Bright Harbour 嘅聯絡人叫 Kelvin，佢負責採購。',
             'The new warehouse is in Kwai Chung, unit 7B, and opens on Monday.']

    def test_pure_chat_not_saved(self):
        for t in self.PURE: self.assertIn('skipped', self.learn(task=t, tool=None), t)
        self.assertFalse(os.path.isdir(os.path.join(kc.md_dir(self.cfg, kc.lib_name(self.cfg)), 'sources')))

    def test_facts_or_tools_trigger(self):
        for t in self.FACTS: self.assertTrue(kc.should_write(t, 0, []), t)
        self.assertTrue(kc.should_write('讀吓呢份檔', 1, ['file content']))
        self.assertFalse(kc.should_write('讀吓呢份檔', 1, ['   ']))

    def test_stated_fact_without_tools_is_searchable(self):
        self.learn(task='打印機 IP 係 10.20.0.45，喺三樓。', tool=None)
        self.assertIn('10.20.0.45', pack.context(self.cfg, '打印機 IP 係幾多？'))

    def test_background_error_does_not_break_delivery(self):
        from lamellar.core import packs
        reg = packs.Registry()
        def boom(*a): raise RuntimeError('disk full')
        reg.hooks['after_delivery'].append(boom)
        self.assertEqual(reg.call('after_delivery', 'answer', {'cfg': self.cfg}, None), [])   # swallowed; the job already delivered


class KL6Source(Base):
    def test_saved_text_is_user_and_tools_never_the_answer(self):
        out = self.learn(); self.assertGreater(out['source_refs'], 0)
        s = pack.context(self.cfg, Q)
        self.assertIn('40 部', s); self.assertNotIn('我建議', s)
        c = self.db(); txt = ' '.join(r[0] for r in c.execute('SELECT text FROM chunk')); c.close()
        self.assertNotIn('我建議', txt)

    def test_privacy_masked_before_store(self):
        tool = TOOL_OUT + '\nKelvin 電話 9123 4567，聯絡 kelvin@brightharbour.com。訂金 HK$ 48,000。系統 password: hunter22'
        self.learn(tool=tool)
        c = self.db(); txt = ' '.join(r[0] for r in c.execute('SELECT text FROM chunk')); c.close()
        for s in ('9123 4567', 'kelvin@brightharbour.com', '48,000', 'hunter22'): self.assertNotIn(s, txt)
        d = os.path.join(kc.md_dir(self.cfg, kc.lib_name(self.cfg)), 'sources')
        md = ''.join(open(os.path.join(d, f), encoding='utf-8').read() for f in os.listdir(d))
        for s in ('9123 4567', 'hunter22'): self.assertNotIn(s, md)

    def test_users_are_private(self):
        self.learn()
        bob = {**self.cfg, 'armor_chat': 'bob'}
        self.assertIn('Bright Harbour', pack.context(self.cfg, Q))
        self.assertEqual(pack.context(bob, Q), '')
        self.assertNotIn('Bright', pack.kal_search({'cfg': bob}, 'Bright Harbour LeArm')[1])
        self.assertNotEqual(kc.lib_name(self.cfg), kc.lib_name(bob))

    def test_rebuild_matches_md(self):
        self.learn(); self.learn(task='Q-2031 有更新', tool='報價單 Q-2031 已改：客戶 Bright Harbour 改為訂 60 部 LeArm 機械臂。')
        dump = lambda: sorted(self.db().execute('SELECT ref, text FROM chunk').fetchall())
        before = dump()
        self.assertEqual(kc.rebuild(self.cfg, kc.lib_name(self.cfg)), {'sources': 2})
        self.assertEqual(dump(), before)
        kal_dir = os.path.dirname(store.lib_path(self.cfg))
        self.assertTrue(any('.bak-' in f for f in os.listdir(kal_dir)))       # old db kept, not deleted


class KL6Hook(Base):
    def test_hook_saves_and_skips(self):
        ctx = {'cfg': self.cfg, 'task': TASK, 'messages': msgs_for(TASK, TOOL_OUT), 'tools_used': 1, 'session': 's'}
        pack.after_delivery(ANSWER, ctx, None); self.assertGreater(ctx['kal_chat']['source_refs'], 0)
        ctx2 = {'cfg': self.cfg, 'task': '你是誰？', 'messages': [{'role': 'user', 'content': '你是誰？'}], 'tools_used': 0}
        pack.after_delivery('我係 lamellar', ctx2, None); self.assertIn('skipped', ctx2['kal_chat'])

    def test_off_when_disabled(self):
        cfg = {**self.cfg, 'kal': {'chat_write': False}}; ctx = {'cfg': cfg, 'task': TASK, 'messages': msgs_for(TASK, TOOL_OUT), 'tools_used': 1}
        pack.after_delivery(ANSWER, ctx, None); self.assertNotIn('kal_chat', ctx)


class KL9Rerank(Base):
    def test_chat_library_is_reranked_by_default(self):
        self.learn(); seen = []
        def fake_rerank(cfg, o, task, cands, deadline):
            seen.append((o['rerank_model'], o['rerank_keep'], len(cands))); return []      # the model says nothing is useful
        old = pack._rerank; pack._rerank = fake_rerank
        try:
            self.assertEqual(pack.context({**self.cfg, 'kal': {'chat_write': True}}, Q), '')
            self.assertTrue(seen and seen[0][:2] == ('m', 3))
        finally: pack._rerank = old


if __name__ == '__main__': unittest.main()


class Note(Base):
    """0.4.0: "記底" is an explicit instruction; a claim of "saved" must be true."""
    def test_cue(self):
        for t in ['記底：觀塘有間陳大餅店', '記底', '幫我記住 Kelvin 鍾意飲奶茶', 'note this: gate code is in the drawer']:
            self.assertTrue(kc.wants_note(t), t)
        for t in ['為什麼無加入筆記指令？', '你記住咗未？', '今日好熱']:
            self.assertFalse(kc.wants_note(t), t)

    def test_short_fact_with_cue_is_saved(self):            # the 陳大餅店 case: 8 chars, no digits, used to be skipped
        out = self.learn(task='記底，觀塘有間陳大餅店', tool=None)
        self.assertNotIn('skipped', out)
        self.assertIn('陳大餅店', pack.kal_search({'cfg': self.cfg}, '陳大餅店')[1])

    def test_bare_cue_keeps_previous_message(self):
        msgs = [{'role': 'user', 'content': '觀塘有間陳大餅店'}, {'role': 'assistant', 'content': '好'},
                {'role': 'user', 'content': '記底'}]
        kc.learn_exchange(self.cfg, '記底', msgs, 0, 's')
        self.assertIn('陳大餅店', pack.kal_search({'cfg': self.cfg}, '陳大餅店')[1])

    def test_kal_note_tool(self):
        ctx = {'cfg': self.cfg, 'kal_task': '記底：觀塘有間陳大餅店', 'session': 's'}
        ok, out = pack.kal_note(ctx, '觀塘有間陳大餅店')
        self.assertTrue(ok); self.assertTrue(out.startswith(kc.NOTED)); self.assertTrue(ctx['kal_noted'])
        self.assertIn('陳大餅店', pack.context({**self.cfg, 'kal': {'chat_write': True, 'chat_rerank': False}}, '觀塘陳大餅店喺邊？'))
        self.assertFalse(pack.kal_note(ctx, '  ')[0])

    def test_kal_note_works_with_chat_write_off(self):
        cfg = {**self.cfg, 'kal': {'chat_write': False, 'chat_rerank': False}}
        pack.kal_note({'cfg': cfg, 'kal_task': '記底 X'}, '觀塘有間陳大餅店')
        self.assertIn('陳大餅店', pack.kal_search({'cfg': cfg}, '陳大餅店')[1])

    def test_no_double_save_after_kal_note(self):
        msgs = [{'role': 'user', 'content': '記底：觀塘有間陳大餅店'},
                {'role': 'tool', 'content': kc.NOTED + ' [chat/x#t1.1]:\n觀塘有間陳大餅店'}]
        self.assertIn('skipped', kc.learn_exchange(self.cfg, '記底：觀塘有間陳大餅店', msgs, 1, 's', noted=True))

    def test_false_saved_claim_bounced(self):
        ctx = {'cfg': self.cfg, 'kal_task': '記底：觀塘有間陳大餅店'}
        self.assertIsNotNone(pack.before_delivery('好，記低咗！', ctx))
        self.assertIsNone(pack.before_delivery('未存到，因為出錯。', ctx))
        ctx['kal_noted'] = True
        self.assertIsNone(pack.before_delivery('好，記低咗！', ctx))
        self.assertIsNone(pack.before_delivery('記低咗', {'cfg': self.cfg, 'kal_task': '今日好熱'}))

    def test_hint_injected(self):
        ctx = {'cfg': {**self.cfg, 'kal': {'inject': False}}}
        self.assertIn('kal_note', pack.before_task('記底：觀塘有間陳大餅店', ctx))
        self.assertIsNone(pack.before_task('今日好熱', ctx))

    def test_default_on(self):
        self.assertTrue(pack.DEFAULTS['chat_write'])
