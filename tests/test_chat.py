"""KL6 gates: conversation -> the user's private KAL library (original text only). Run from the pack dir: python3 -m unittest discover -s tests"""
import os, shutil, sys, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
for p in (os.environ.get('LAMELLAR_CORE', ''), os.path.join(os.path.dirname(HERE), 'lamellar')):
    if p and os.path.isdir(p): sys.path.insert(0, p)
import json
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures.json'), encoding='utf-8') as f: T = json.load(f)   # Chinese test text
import pack
from kalcore import store, chat as kc

TOOL_OUT = (T['chat_module_1'] +
            T['chat_module_2'])
TASK = T['chat_module_3']
ANSWER = T['chat_module_4']
Q = T['chat_module_5']


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
    PURE = [T['chat_module_6'], 'hi', T['chat_module_7'], T['chat_module_8'], T['chat_module_9'], T['chat_module_10'], T['chat_module_11'], T['chat_module_12'], T['chat_module_13'], 'OK']
    FACTS = [T['chat_module_14'], T['chat_module_15'],
             'The new warehouse is in Kwai Chung, unit 7B, and opens on Monday.']

    def test_pure_chat_not_saved(self):
        for t in self.PURE: self.assertIn('skipped', self.learn(task=t, tool=None), t)
        self.assertFalse(os.path.isdir(os.path.join(kc.md_dir(self.cfg, kc.lib_name(self.cfg)), 'sources')))

    def test_facts_or_tools_trigger(self):
        for t in self.FACTS: self.assertTrue(kc.should_write(t, 0, []), t)
        self.assertTrue(kc.should_write(T['chat_facts_or_tools_trigger_1'], 1, ['file content']))
        self.assertFalse(kc.should_write(T['chat_facts_or_tools_trigger_2'], 1, ['   ']))

    def test_stated_fact_without_tools_is_searchable(self):
        self.learn(task=T['chat_stated_fact_without_tools_is_searchable_1'], tool=None)
        self.assertIn('10.20.0.45', pack.context(self.cfg, T['chat_stated_fact_without_tools_is_searchable_2']))

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
        self.assertIn(T['chat_saved_text_is_user_and_tools_never_the_answer_1'], s); self.assertNotIn(T['chat_saved_text_is_user_and_tools_never_the_answer_2'], s)
        c = self.db(); txt = ' '.join(r[0] for r in c.execute('SELECT text FROM chunk')); c.close()
        self.assertNotIn(T['chat_saved_text_is_user_and_tools_never_the_answer_3'], txt)

    def test_privacy_masked_before_store(self):
        tool = TOOL_OUT + T['chat_privacy_masked_before_store_1']
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
        self.learn(); self.learn(task=T['chat_rebuild_matches_md_1'], tool=T['chat_rebuild_matches_md_2'])
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
        ctx2 = {'cfg': self.cfg, 'task': T['chat_hook_saves_and_skips_1'], 'messages': [{'role': 'user', 'content': T['chat_hook_saves_and_skips_2']}], 'tools_used': 0}
        pack.after_delivery(T['chat_hook_saves_and_skips_3'], ctx2, None); self.assertIn('skipped', ctx2['kal_chat'])

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
    T['chat_module_16']
    def test_cue(self):
        for t in [T['chat_cue_1'], T['chat_cue_2'], T['chat_cue_3'], 'note this: gate code is in the drawer']:
            self.assertTrue(kc.wants_note(t), t)
        for t in [T['chat_cue_4'], T['chat_cue_5'], T['chat_cue_6']]:
            self.assertFalse(kc.wants_note(t), t)

    def test_short_fact_with_cue_is_saved(self):            # the bakery case (11-18): 8 chars, no digits, used to be skipped
        out = self.learn(task=T['chat_short_fact_with_cue_is_saved_1'], tool=None)
        self.assertNotIn('skipped', out)
        self.assertIn(T['chat_short_fact_with_cue_is_saved_2'], pack.kal_search({'cfg': self.cfg}, T['chat_short_fact_with_cue_is_saved_3'])[1])

    def test_bare_cue_keeps_previous_message(self):
        msgs = [{'role': 'user', 'content': T['chat_bare_cue_keeps_previous_message_1']}, {'role': 'assistant', 'content': T['chat_bare_cue_keeps_previous_message_2']},
                {'role': 'user', 'content': T['chat_bare_cue_keeps_previous_message_3']}]
        kc.learn_exchange(self.cfg, T['chat_bare_cue_keeps_previous_message_4'], msgs, 0, 's')
        self.assertIn(T['chat_bare_cue_keeps_previous_message_5'], pack.kal_search({'cfg': self.cfg}, T['chat_bare_cue_keeps_previous_message_6'])[1])

    def test_kal_note_tool(self):
        ctx = {'cfg': self.cfg, 'kal_task': T['chat_kal_note_tool_1'], 'session': 's'}
        ok, out = pack.kal_note(ctx, T['chat_kal_note_tool_2'])
        self.assertTrue(ok); self.assertTrue(out.startswith(kc.NOTED)); self.assertTrue(ctx['kal_noted'])
        self.assertIn(T['chat_kal_note_tool_3'], pack.context({**self.cfg, 'kal': {'chat_write': True, 'chat_rerank': False}}, T['chat_kal_note_tool_4']))
        self.assertFalse(pack.kal_note(ctx, '  ')[0])

    def test_kal_note_own_choice(self):
        """0.5.0: the model may keep what the user said on its own judgement, but only the user's words."""
        cfg = {**self.cfg, 'kal': {'chat_write': True, 'chat_rerank': False}}
        msgs = [{'role': 'user', 'content': T['chat_kal_note_own_choice_1']}]
        ok, out = pack.kal_note({'cfg': cfg, 'kal_task': T['chat_kal_note_own_choice_2'], 'messages': msgs, 'session': 's'}, T['chat_kal_note_own_choice_3'])
        self.assertTrue(ok, out); self.assertIn('own choice', out); self.assertIn('chat/auto-', out)
        self.assertIn(T['chat_kal_note_own_choice_4'], pack.kal_search({'cfg': cfg}, T['chat_kal_note_own_choice_5'])[1])
        ok, out = pack.kal_note({'cfg': cfg, 'kal_task': T['chat_kal_note_own_choice_6'], 'messages': msgs}, T['chat_kal_note_own_choice_7'])
        self.assertFalse(ok); self.assertIn("user's own words", out)                 # invented: refused
        off = {**self.cfg, 'kal': {'chat_write': False}}
        self.assertFalse(pack.kal_note({'cfg': off, 'kal_task': T['chat_kal_note_own_choice_8'], 'messages': msgs}, T['chat_kal_note_own_choice_9'])[0])

    def test_kal_note_not_from_answer(self):
        T['chat_kal_note_not_from_answer_1']
        ok, out = pack.kal_note({'cfg': self.cfg, 'kal_task': T['chat_kal_note_not_from_answer_2']}, T['chat_kal_note_not_from_answer_3'])
        self.assertFalse(ok)
        self.assertNotIn(T['chat_kal_note_not_from_answer_4'], pack.kal_search({'cfg': self.cfg}, T['chat_kal_note_not_from_answer_5'])[1])
        self.assertTrue(pack.kal_note({'cfg': self.cfg, 'kal_task': T['chat_kal_note_not_from_answer_6']}, T['chat_kal_note_not_from_answer_7'])[0])
        self.assertTrue(pack.claims_library(T['chat_kal_note_not_from_answer_8']))          # run 11 after kal_note was refused
        self.assertFalse(pack.claims_library(T['chat_kal_note_not_from_answer_9']))
        self.assertTrue(pack.claims_library(T['chat_kal_note_not_from_answer_10']))   # run 22
        self.assertFalse(pack.claims_library(T['chat_kal_note_not_from_answer_11']))
        self.assertTrue(pack.claims_library(T['chat_kal_note_not_from_answer_12']))

    def test_kal_note_works_with_chat_write_off(self):
        cfg = {**self.cfg, 'kal': {'chat_write': False, 'chat_rerank': False}}
        pack.kal_note({'cfg': cfg, 'kal_task': T['chat_kal_note_works_with_chat_write_off_1']}, T['chat_kal_note_works_with_chat_write_off_2'])
        self.assertIn(T['chat_kal_note_works_with_chat_write_off_3'], pack.kal_search({'cfg': cfg}, T['chat_kal_note_works_with_chat_write_off_4'])[1])

    def test_no_double_save_after_kal_note(self):
        msgs = [{'role': 'user', 'content': T['chat_no_double_save_after_kal_note_1']},
                {'role': 'tool', 'content': kc.NOTED + T['chat_no_double_save_after_kal_note_2']}]
        self.assertIn('skipped', kc.learn_exchange(self.cfg, T['chat_no_double_save_after_kal_note_3'], msgs, 1, 's', noted=True))

    def test_false_saved_claim_bounced(self):
        ctx = {'cfg': self.cfg, 'kal_task': T['chat_false_saved_claim_bounced_1']}
        self.assertIsNotNone(pack.before_delivery(T['chat_false_saved_claim_bounced_2'], ctx))
        self.assertIsNone(pack.before_delivery(T['chat_false_saved_claim_bounced_3'], ctx))
        ctx['kal_noted'] = True
        self.assertIsNone(pack.before_delivery(T['chat_false_saved_claim_bounced_4'], ctx))
        self.assertIsNone(pack.before_delivery(T['chat_false_saved_claim_bounced_5'], {'cfg': self.cfg, 'kal_task': T['chat_false_saved_claim_bounced_6']}))

    def test_library_claim_after_empty_search_bounced(self):
        ctx = {'cfg': {**self.cfg, 'kal': {'inject': False}}}
        pack.before_task(T['chat_library_claim_after_empty_search_bounced_1'], ctx)
        ok, out = pack.kal_search(ctx, T['chat_library_claim_after_empty_search_bounced_2'])
        self.assertIn('nothing in the library', out)
        bad = T['chat_library_claim_after_empty_search_bounced_3']
        self.assertIsNotNone(pack.before_delivery(bad, ctx))
        self.assertIsNone(pack.before_delivery(T['chat_library_claim_after_empty_search_bounced_4'], ctx))
        self.assertIsNone(pack.before_delivery(T['chat_library_claim_after_empty_search_bounced_5'], ctx))
        ctx['kal_seen'] = [T['chat_library_claim_after_empty_search_bounced_6']]                       # 11-18 case: a hit, but only the user's own question
        self.assertIn(T['chat_library_claim_after_empty_search_bounced_7'], pack.before_delivery(bad, ctx))
        ctx['kal_seen'] = [T['chat_library_claim_after_empty_search_bounced_8']]    # the library really has it: claim allowed
        self.assertIsNone(pack.before_delivery(bad, ctx))
        self.assertIn('kal_search', pack.before_delivery(bad, {'cfg': self.cfg, 'kal_task': 'x'}))   # no lookup at all: go and check
        self.assertIsNone(pack.before_delivery(T['chat_library_claim_after_empty_search_bounced_9'], {'cfg': self.cfg, 'kal_seen': ['something']}))   # no quote: any evidence will do
        fixed = (T['chat_library_claim_after_empty_search_bounced_10'] +
                 T['chat_library_claim_after_empty_search_bounced_11'])
        listed = T['chat_library_claim_after_empty_search_bounced_12']   # 11-18 run 7: name on the next line, bold
        self.assertEqual(pack.unsupported(listed, [T['chat_library_claim_after_empty_search_bounced_13']]), T['chat_library_claim_after_empty_search_bounced_14'])
        self.assertIsNone(pack.unsupported(listed, [T['chat_library_claim_after_empty_search_bounced_15']]))
        self.assertIsNone(pack.before_delivery(fixed,{'cfg': self.cfg, 'kal_searched': True, 'kal_seen': [T['chat_library_claim_after_empty_search_bounced_16']]}))

    def test_no_note_after_source_bounce(self):
        ctx = {'cfg': self.cfg, 'kal_task': T['chat_no_note_after_source_bounce_1'], 'kal_searched': True, 'kal_seen': []}
        self.assertIsNotNone(pack.before_delivery(T['chat_no_note_after_source_bounce_2'], ctx))
        ok, out = pack.kal_note(ctx, T['chat_no_note_after_source_bounce_3'])
        self.assertFalse(ok); self.assertNotIn(T['chat_no_note_after_source_bounce_4'], pack.kal_search({'cfg': self.cfg}, T['chat_no_note_after_source_bounce_5'])[1])

    def test_claims_library(self):
        self.assertTrue(pack.claims_library('My notes show you like egg tarts.'))
        self.assertTrue(pack.claims_library(T['chat_claims_library_1']))
        self.assertFalse(pack.claims_library('The library has nothing on that.'))
        self.assertFalse(pack.claims_library(T['chat_claims_library_2']))
        self.assertTrue(pack.claims_library(T['chat_claims_library_3']))
        self.assertEqual(pack.unsupported(T['chat_claims_library_4'], [T['chat_claims_library_5']]), T['chat_claims_library_6'])
        self.assertFalse(pack.claims_library(T['chat_claims_library_7']))

    def test_hint_injected(self):
        ctx = {'cfg': {**self.cfg, 'kal': {'inject': False}}}
        self.assertIn('kal_note', pack.before_task(T['chat_hint_injected_1'], ctx))
        self.assertIsNone(pack.before_task(T['chat_hint_injected_2'], ctx))
        self.assertIn('own words', pack.before_task(T['chat_hint_injected_3'], ctx))
        self.assertIsNone(pack.before_task(T['chat_hint_injected_4'], ctx))

    def test_default_on(self):
        self.assertTrue(pack.DEFAULTS['chat_write'])
