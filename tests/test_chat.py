"""KL6-KL8 gates: conversation -> private KAL notes. Run from the pack dir: python3 -m unittest discover -s tests"""
import json, os, shutil, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
for p in (os.environ.get('LAMELLAR_CORE', ''), os.path.join(os.path.dirname(HERE), 'lamellar')):
    if p and os.path.isdir(p): sys.path.insert(0, p)
import pack
from kalcore import store, chat as kc

TOOL_OUT = ("報價單 Q-2031：客戶 Bright Harbour 訂 40 部 LeArm 機械臂，交貨期 2026-12-05。\n"
            "倉存表顯示 LeArm 現貨 25 部，另外 15 部要向深圳廠補貨。")
TASK = '幫我睇吓 quotes/Q-2031.txt 份報價單，交唔交得切貨？'
ANSWER = '份報價單要 40 部，現貨得 25 部，所以要補 15 部。我建議即日落單補貨，應該趕得切 12 月 5 號。'


def item(**kw):
    base = {'op': 'add', 'replaces': '', 'title': 'Q-2031 Bright Harbour 訂 40 部 LeArm', 'body': 'Bright Harbour 訂 40 部，交貨期 2026-12-05。',
            'quotes': ['客戶 Bright Harbour 訂 40 部 LeArm 機械臂，交貨期 2026-12-05。'], 'refs': ['R#t1.1'], 'terms': ['Bright Harbour', 'LeArm'], 'links': []}
    base.update(kw); return base


def fake(replies):
    """A model that returns the given JSON replies in order and counts calls."""
    state = {'calls': 0, 'prompts': []}
    def chat(msgs):
        state['prompts'].append(msgs[-1]['content'])
        r = replies[min(state['calls'], len(replies) - 1)]; state['calls'] += 1
        return r(msgs) if callable(r) else json.dumps(r, ensure_ascii=False)
    chat.state = state
    return chat


def msgs_for(task, tool_out):
    return [{'role': 'user', 'content': task + '\n\n[kal] ...'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1'}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': tool_out}]


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.cfg = {'model': 'm', 'context': 8192, 'state_dir': self.t, 'max_output_tokens': 100, 'endpoint': 'http://127.0.0.1:9/v1',
                    'armor_bot': 'web', 'armor_chat': 'alice', 'kal': {'chat_write': True}}

    def tearDown(self): shutil.rmtree(self.t)

    def learn(self, replies, task=TASK, tool=TOOL_OUT, answer=ANSWER, cfg=None, now=None, tools_used=1):
        """Run one exchange; the fake model fills in the real refs of the saved source."""
        cfg = cfg or self.cfg
        def fix_refs(r):
            def f(msgs):
                import re
                refs = re.findall(r'^\[(chat/[^\]]+)\]', msgs[0]['content'], re.M)
                tref = next((x for x in refs if '#t' in x), refs[0] if refs else '')
                uref = next((x for x in refs if '#u' in x), '')
                txt = json.dumps(r, ensure_ascii=False).replace('R#t1.1', tref).replace('R#u1', uref)
                return txt
            return f
        ch = fake([fix_refs(r) for r in replies])
        out = kc.learn_exchange(cfg, ch, task, answer, msgs_for(task, tool) if tool else [{'role': 'user', 'content': task}],
                                tools_used if tool else 0, 'sess1', now=now)
        return out, ch

    def db(self, cfg=None): return store.connect(store.lib_path(cfg or self.cfg, kc.lib_name(cfg or self.cfg)))


class KL6Trigger(Base):
    PURE = ['你是誰？', 'hi', '你有記憶嗎？', 'The North Face的產品耐用嗎？', '多謝', '你能寫程式嗎？', '在嗎？', '你是什麼model?', 'tnf t shirt耐用嗎？', 'OK']
    FACTS = ['我哋公司用 Xero 做帳，月結喺每月 5 號。', 'Bright Harbour 嘅聯絡人叫 Kelvin，佢負責採購。',
             'The new warehouse is in Kwai Chung, unit 7B, and opens on Monday.']

    def test_pure_chat_never_calls_model(self):
        for t in self.PURE:
            out, ch = self.learn([{'items': []}], task=t, tool=None)
            self.assertEqual(ch.state['calls'], 0, t); self.assertIn('skipped', out, t)

    def test_facts_or_tools_trigger(self):
        for t in self.FACTS: self.assertTrue(kc.should_write(t, 0, []), t)
        self.assertTrue(kc.should_write('讀吓呢份檔', 1, ['file content']))
        self.assertFalse(kc.should_write('讀吓呢份檔', 1, ['   ']))

    def test_three_routes(self):
        # facts -> kal note; preference / correction -> refused here (core memory / correction capture keep those)
        pref = item(title='用戶偏好', body='以後報價單一律用繁體中文。', quotes=['以後報價單一律用繁體中文'], refs=['R#u1'], terms=['報價單'])
        out, _ = self.learn([{'items': [item(), pref]}, {'items': [pref]}, {'items': [pref]}],
                            task='以後報價單一律用繁體中文。幫我睇吓 quotes/Q-2031.txt。')
        self.assertEqual(len(out['written']), 1); self.assertEqual(out['rejected'], 1)
        from lamellar.armor.capture import CUE
        self.assertTrue(CUE.search('From now on always write quotes in Traditional Chinese'))   # correction route is the core's, unchanged

    def test_privacy_list_blocked(self):
        bad = [item(body='Kelvin 電話 9123 4567。', quotes=['Kelvin 電話 9123 4567'], terms=['Kelvin']),
               item(body='聯絡 kelvin@brightharbour.com。', quotes=['聯絡 kelvin@brightharbour.com'], terms=['Kelvin']),
               item(body='訂金 HK$ 48,000。', quotes=['訂金 HK$ 48,000'], terms=['訂金']),
               item(body='系統 password: hunter22。', quotes=['password: hunter22'], terms=['password'])]
        tool = TOOL_OUT + '\nKelvin 電話 9123 4567，聯絡 kelvin@brightharbour.com。訂金 HK$ 48,000。系統 password: hunter22'
        for b in bad:
            out, _ = self.learn([{'items': [b]}] * 3, tool=tool)
            self.assertEqual(out['written'], [], b['body'])
        c = self.db(); txt = ' '.join(r[0] for r in c.execute('SELECT text FROM chunk')); c.close()
        for s in ('9123 4567', 'kelvin@brightharbour.com', '48,000', 'hunter22'): self.assertNotIn(s, txt)   # masked in the stored source too

    def test_background_error_does_not_break_delivery(self):
        from lamellar.core import packs
        reg = packs.Registry()
        def boom(*a): raise RuntimeError('model down')
        reg.hooks['after_delivery'].append(boom)
        self.assertEqual(reg.call('after_delivery', 'answer', {'cfg': self.cfg}, None), [])   # swallowed; the job already delivered


class KL7Write(Base):
    def test_quote_from_own_answer_rejected(self):
        own = [item(quotes=['現貨得 25 部，所以要補 15 部'], body='要補 15 部。'),
               item(quotes=['我建議即日落單補貨，應該趕得切 12 月 5 號'], body='即日落單補貨。', terms=['補貨']),
               item(quotes=['應該趕得切'], body='趕得切交貨。', terms=['交貨'])]
        for o in own:
            out, ch = self.learn([{'items': [o]}] * 3)
            self.assertEqual(out['written'], [], o['quotes'])
            why = kc.qc_item(dict(o, refs=['chat/x#t1.1']), {'chat/x#t1.1': TOOL_OUT}, ANSWER, [], {})
            self.assertIn("assistant's own answer", why)
        passages = ch.state['prompts'][0].split('Passages:')[1].split("Assistant's final answer")[0]
        self.assertNotIn('我建議', passages)                                     # the answer is never offered as a passage

    def test_good_note_written_low_trust_with_md(self):
        out, _ = self.learn([{'items': [item()]}])
        self.assertEqual(len(out['written']), 1)
        c = self.db(); r = c.execute('SELECT origin, trust, n_used FROM node').fetchone(); c.close()
        self.assertEqual(r, ('chat', 'low', 1))
        self.assertTrue(os.path.isfile(os.path.join(kc.md_dir(self.cfg, kc.lib_name(self.cfg)), 'nodes', out['written'][0] + '.md')))

    def test_update_supersedes_never_deletes(self):
        out1, _ = self.learn([{'items': [item()]}]); old = out1['written'][0]
        tool2 = '報價單 Q-2031 已改：客戶 Bright Harbour 改為訂 60 部 LeArm 機械臂，交貨期不變。'
        upd = item(op='update', replaces=old, title='Q-2031 改為 60 部', body='Bright Harbour 改為訂 60 部。',
                   quotes=['客戶 Bright Harbour 改為訂 60 部 LeArm 機械臂'], terms=['Bright Harbour'])
        out2, ch = self.learn([{'items': [upd]}], task='Q-2031 有更新，幫我睇吓 quotes/Q-2031.txt。', tool=tool2)
        self.assertIn(old, ch.state['prompts'][0])                           # write-before-search showed the old note
        self.assertEqual(out2['updated'], [old])
        c = self.db(); rows = dict(c.execute('SELECT id, status FROM node').fetchall())
        sup = c.execute('SELECT superseded_by FROM node WHERE id=?', (old,)).fetchone()[0]; c.close()
        self.assertEqual(rows[old], 'superseded'); self.assertEqual(rows[out2['written'][0]], 'ok'); self.assertEqual(sup, out2['written'][0])
        ctx = pack.context(self.cfg, 'Bright Harbour 訂幾多部 LeArm？')
        self.assertIn('60', ctx); self.assertNotIn('Q-2031 Bright Harbour 訂 40 部', ctx)

    def test_users_are_private(self):
        self.learn([{'items': [item()]}])
        bob = {**self.cfg, 'armor_chat': 'bob'}
        self.assertIn('Bright Harbour', pack.context(self.cfg, 'Bright Harbour 訂咗幾多部 LeArm？'))
        self.assertEqual(pack.context(bob, 'Bright Harbour 訂咗幾多部 LeArm？'), '')
        ok, out = pack.kal_search({'cfg': bob}, 'Bright Harbour LeArm')
        self.assertNotIn('Bright', out)
        self.assertNotEqual(kc.lib_name(self.cfg), kc.lib_name(bob))

    def test_from_conversation_tag(self):
        self.learn([{'items': [item()]}])
        s = pack.context(self.cfg, 'Bright Harbour 訂咗幾多部 LeArm？')
        self.assertIn('(from an earlier conversation, not yet confirmed)', s)


class KL8Brain(Base):
    def test_rebuild_matches_md(self):
        out1, _ = self.learn([{'items': [item()]}])
        upd = item(op='update', replaces=out1['written'][0], title='Q-2031 改為 60 部', body='Bright Harbour 改為訂 60 部。',
                   quotes=['客戶 Bright Harbour 改為訂 60 部 LeArm 機械臂'], terms=['Bright Harbour'], links=[])
        self.learn([{'items': [upd]}], task='Q-2031 有更新', tool='報價單 Q-2031 已改：客戶 Bright Harbour 改為訂 60 部 LeArm 機械臂，交貨期不變。')
        lib = kc.lib_name(self.cfg)
        kc.reinforce(self.cfg, lib, [dict(n, lib=lib) for n in pack._context(self.cfg, 'Bright Harbour 60 部')[1]], 'Bright Harbour 改為訂 60 部')
        def dump():
            c = self.db()
            d = (sorted(c.execute('SELECT id, title, body, quotes, refs, terms, origin, status, n_used, last_used, created, trust, session, superseded_by FROM node')),
                 sorted(c.execute('SELECT src, dst, round(weight, 4) FROM edge')), sorted(c.execute('SELECT ref, text FROM chunk')))
            c.close(); return d
        before = dump(); res = kc.rebuild(self.cfg, lib); after = dump()
        self.assertEqual(before, after); self.assertEqual(res['nodes'], 2)
        self.assertTrue(any(f.startswith(lib + '.db.bak-') for f in os.listdir(os.path.join(self.t, 'kal'))))   # old db kept
        self.assertIn('60', pack.context(self.cfg, 'Bright Harbour 訂幾多部？'))

    def test_unused_note_fades_below_threshold(self):
        t0 = time.time()
        self.assertAlmostEqual(store.activation(1, t0, 'low', t0), 0.7)
        rel_now = 0.6 * store.activation(1, t0, 'low', t0)
        rel_90 = 0.6 * store.activation(1, t0, 'low', t0 + 90 * 86400)
        self.assertGreater(rel_now, 0.05); self.assertLess(rel_90, 0.05)
        self.assertEqual(store.activation(1, t0, 'normal', t0 + 900 * 86400, origin='digest'), 1.0)   # documents do not fade
        out, _ = self.learn([{'items': [item()]}], now=t0 - 400 * 86400)
        self.assertEqual(pack.context(self.cfg, 'Bright Harbour 訂咗幾多部 LeArm？').count('from an earlier conversation'), 0)

    def test_found_not_cited_and_hops_not_strengthened(self):
        a, _ = self.learn([{'items': [item()]}])
        b_item = item(title='LeArm 現貨 25 部', body='LeArm 現貨 25 部，另外 15 部要向深圳廠補貨。', quotes=['倉存表顯示 LeArm 現貨 25 部'],
                      terms=['LeArm'], links=[a['written'][0]])
        b, _ = self.learn([{'items': [b_item]}], task='倉存表 stock.csv 入面 LeArm 有幾多？')
        lib = kc.lib_name(self.cfg)
        txt, shown = pack._context(self.cfg, 'Bright Harbour 訂單')
        shown = [dict(n, lib=lib) for n in shown]
        self.assertEqual(kc.reinforce(self.cfg, lib, shown, '我唔知。'), [])                                          # found, not cited
        hop = [dict(n, hop=1) for n in shown]
        self.assertEqual(kc.reinforce(self.cfg, lib, hop, f"見 {a['written'][0]} 同 {b['written'][0]}"), [])          # hop notes never
        c = self.db(); self.assertEqual({r[0] for r in c.execute('SELECT n_used FROM node')}, {1}); c.close()

    def test_cited_strengthens_capped_and_promotes(self):
        a, _ = self.learn([{'items': [item()]}])
        b, _ = self.learn([{'items': [item(title='LeArm 現貨 25 部', body='LeArm 現貨 25 部。', quotes=['倉存表顯示 LeArm 現貨 25 部'], terms=['LeArm'])]}],
                          task='stock.csv 入面 LeArm 有幾多？')
        lib = kc.lib_name(self.cfg); ids = a['written'] + b['written']
        for _ in range(10):
            shown = [dict(n, lib=lib) for n in pack._context(self.cfg, 'Bright Harbour LeArm 訂單 現貨 25 部')[1]]
            kc.reinforce(self.cfg, lib, shown, ' '.join(ids))
        c = self.db()
        self.assertLessEqual(max(r[0] for r in c.execute('SELECT weight FROM edge')), kc.MAX_W)
        self.assertEqual({r[0] for r in c.execute('SELECT trust FROM node')}, {'normal'})
        c.close()
        kc.decay(self.cfg, lib)
        c = self.db(); self.assertAlmostEqual(max(r[0] for r in c.execute('SELECT weight FROM edge')), kc.MAX_W * kc.EDGE_DECAY); c.close()

    def test_no_group_hogs_twenty_questions(self):
        """20 notes on 20 different topics, all linked in a chain; 20 different questions, each answer cites what it was shown.
        No set of notes may top more than 3 questions, and no edge goes above MAX_W (the Sept 2026 runaway)."""
        lib = kc.lib_name(self.cfg)
        topics = [('Xero', '帳目用 Xero'), ('Kwai Chung', '倉喺 Kwai Chung'), ('LeArm', 'LeArm 機械臂'), ('Kelvin', 'Kelvin 管採購'),
                  ('Q-2031', 'Q-2031 報價'), ('Rust', 'Rust 編譯'), ('Postgres', 'Postgres 備份'), ('PyPI', 'PyPI 發版'),
                  ('Ollama', 'Ollama 版本'), ('Telegram', 'Telegram bot'), ('MySQL', 'MySQL 索引'), ('Webcam', 'Webcam 鏡頭'),
                  ('Router', 'Router 設定'), ('Invoice', 'Invoice 格式'), ('Payroll', 'Payroll 月結'), ('Printer', 'Printer 碳粉'),
                  ('Docker', 'Docker 映像'), ('YOLO', 'YOLO 偵測'), ('Cloudflare', 'Cloudflare 上傳'), ('Nginx', 'Nginx 設定')]
        c = self.db(); prev = None; now = int(time.time())
        for i, (term, title) in enumerate(topics):
            ref = f'chat/x{i}#u1'
            kc._insert_source(c, f'chat/x{i}', 's', now, {ref: f'{title}：第 {i} 項事實，{term} 嘅資料。'})
            nid = store.add_node(c, {'title': title, 'body': f'{term} 第 {i} 項', 'quotes': [f'{title}：第 {i} 項事實'], 'refs': [ref], 'terms': [term]},
                                 origin='chat', trust='low', now=now)
            if prev: kc._link(c, nid, prev)
            prev = nid
        c.commit(); c.close()
        tops = []
        for term, title in topics:
            txt, shown = pack._context(self.cfg, f'{term} 嘅資料係乜？')
            shown = [dict(n, lib=lib) for n in shown]
            tops.append(tuple(sorted(n['id'] for n in shown if not n.get('hop'))))
            kc.reinforce(self.cfg, lib, shown, ' '.join(n['id'] for n in shown))
        from collections import Counter
        self.assertLessEqual(max(Counter(tops).values()), 3, Counter(tops).most_common(2))
        c = self.db(); self.assertLessEqual(max(r[0] for r in c.execute('SELECT weight FROM edge')), kc.MAX_W); c.close()


class KL9SourceOnly(Base):
    def test_source_only_never_calls_model_and_is_searchable(self):
        ch = fake([{'items': [item()]}])
        out = kc.learn_exchange(self.cfg, ch, TASK, ANSWER, msgs_for(TASK, TOOL_OUT), 1, 's', nodes=False)
        self.assertEqual(ch.state['calls'], 0); self.assertEqual(out['mode'], 'source only')
        s = pack.context(self.cfg, 'Bright Harbour 訂咗幾多部 LeArm？')
        self.assertIn('40 部', s); self.assertNotIn('我建議', s)          # the user's / tools' words, never the answer
        self.assertEqual(pack.context({**self.cfg, 'armor_chat': 'bob'}, 'Bright Harbour 訂咗幾多部 LeArm？'), '')

    def test_hook_default_is_source_only(self):
        ctx = {'cfg': self.cfg, 'task': TASK, 'messages': msgs_for(TASK, TOOL_OUT), 'tools_used': 1, 'session': 's'}
        pack.after_delivery(ANSWER, ctx, None)
        self.assertEqual(ctx['kal_chat']['mode'], 'source only')
        ctx2 = {'cfg': self.cfg, 'task': '你是誰？', 'messages': [{'role': 'user', 'content': '你是誰？'}], 'tools_used': 0}
        pack.after_delivery('我係 lamellar', ctx2, None); self.assertIn('skipped', ctx2['kal_chat'])


if __name__ == '__main__': unittest.main()
