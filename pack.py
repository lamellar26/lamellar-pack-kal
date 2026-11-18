"""kal: knowledge armor. Your folders become a local library the model can search. Fully offline, standard library only.

Config (worker.json):
  "kal": {"libs": ["default"], "max_chars": 3000, "min_rel": 0.05, "rel_top": 0.5, "k": 5, "inject": true,
          "rerank_model": "", "rerank_keep": 5, "timeout_s": 30, "chat_write": true, "chat_rerank": true, "chat_rerank_keep": 3}
  chat_write: after each delivered answer that used tools or stated facts, what the user said and what tools returned is
  saved to the user's private library (chat-<bot>-<chat>.db, see kalcore/chat.py), searched only for the same user.
  chat_rerank: conversation passages are long tool outputs, so word overlap alone lets unrelated questions in (6 of 10 in KL9);
  each candidate is scored by the model (rerank_model, else the main model) and kept at >= chat_rerank_keep (0 of 10 in KL9).
  chat_write is on by default (since 0.4.0). In a group chat it keeps what everyone in the group said; set it to false there
  if the group has not agreed.
  kal_note: when the user says "note this" / "note this", the model saves the user's words with kal_note. If it does not, the
  after-delivery step saves the message anyway (a bare "note this" also keeps the message before it). An answer that says
  "saved" when nothing was saved is sent back to the model (before_delivery). kal_note works even with chat_write off.
  Own choice (0.5.0): with chat_write on, the model may also call kal_note by itself for what it judges worth keeping
  (preferences, decisions, facts about the user or their work). Such a note must be the user's own words: at least
  NOTE_GROUND of its 3-grams must appear in what the user said this session, else it is refused (KL9: a writer model
  invents details). It is stored as chat/auto-*, and the model is told to say in one line what it kept.
  Never while fixing a wrong source claim (a source fix must not turn into a save).
  Source check (0.4.1): an answer that says the library has something must match what the library returned this turn
  (kal_search / kal_read / kal_note / injected material); quoted or bold names are checked one by one. If it came from
  earlier in the conversation, the answer is sent back to say so.
Notes digested by a model were removed 11-17: they added nothing over the original passages (KL4) and invented details (KL9).
"""
import os, re, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path: sys.path.insert(0, HERE)
from kalcore import store, chat as kchat
from kalcore.words import alt

DEFAULTS = {'libs': ['default'], 'max_chars': 3000, 'min_rel': 0.05, 'rel_top': 0.5, 'k': 5, 'inject': True,
            'rerank_model': '', 'rerank_keep': 5, 'timeout_s': 30, 'chat_write': True, 'chat_rerank': True, 'chat_rerank_keep': 3}


def opts(cfg): return {**DEFAULTS, **(cfg.get('kal') or {})}


def _libs(cfg, user=True):
    """The configured libraries, plus this user's own conversation library (never another user's)."""
    names = [l for l in opts(cfg)['libs'] if not l.startswith('chat-')]
    if user and (opts(cfg)['chat_write'] or os.path.isfile(store.lib_path(cfg, kchat.lib_name(cfg)))): names.append(kchat.lib_name(cfg))
    for lib in names:
        p = store.lib_path(cfg, lib)
        if os.path.isfile(p): yield lib, store.connect(p)


def _chat(cfg, model_name, max_tokens=4000, timeout=600):
    from lamellar.core import model
    c2 = {**cfg, 'model': model_name or cfg['model'], 'max_output_tokens': max_tokens, 'think': False, 'request_timeout': timeout}
    return lambda msgs: model.chat(c2, msgs)['content']


def _rerank(cfg, o, task, cands, deadline):
    """Each candidate gets its own request (batching made them interfere, KAL 09-21). Score 0-10; keep >= rerank_keep."""
    chat = _chat(cfg, o['rerank_model'], max_tokens=8, timeout=20); keep = []
    for cd in cands:
        if time.time() > deadline: break
        try:
            out = chat([{'role': 'user', 'content': f'Question:\n{task[:1500]}\n\nPassage:\n{cd["text"][:1500]}\n\n'
                         'How useful is the passage for answering the question? Reply with one number 0-10 only.'}])
            n = int(''.join(ch for ch in out if ch.isdigit())[:2] or 0)
        except Exception: n = 0
        if n >= o['rerank_keep']: keep.append(cd)
    return keep


def context(cfg, task):
    """The passages added to the task. Empty string if nothing is relevant."""
    o = opts(cfg); deadline = time.time() + o['timeout_s']; chunks = []
    for lib, c in _libs(cfg):
        found = [dict(x, lib=lib) for x in store.search(c, task, k=o['k'], min_rel=o['min_rel'], rel_top=o['rel_top'])]
        c.close()
        if lib.startswith('chat-') and o['chat_rerank'] and not o['rerank_model'] and found:
            found = _rerank(cfg, {**o, 'rerank_model': cfg.get('model', ''), 'rerank_keep': o['chat_rerank_keep']}, task, found, deadline)
        chunks += found
    chunks.sort(key=lambda x: -x['rel'])
    if o['rerank_model'] and chunks: chunks = _rerank(cfg, o, task, chunks, deadline)
    if not chunks: return ''
    parts = ['[kal] Material from the user\'s own library that may help. Use it only if it is relevant; cite the [ref] when you use it. '
             'kal_read(ref) shows a passage in full, kal_search(query) searches the library.']
    used = len(parts[0])
    for x in chunks:
        s = f"\n[{x['ref']}]\n{x['text']}"
        if used + len(s) > o['max_chars']:
            room = o['max_chars'] - used - len(x['ref']) - 30
            if room > 200: parts.append(f"\n[{x['ref']}]\n{x['text'][:room]}\u2026")
            break
        parts.append(s); used += len(s)
    return ''.join(parts) if len(parts) > 1 else ''


NOTE_HINT = ('[kal] The user asked you to keep something. Call kal_note with the user\'s own words (what they asked to keep; '
             'if they only said "note this", the message before it). Do not add, guess or summarise. Tell the user it is saved only if '
             'kal_note answered "kal_note saved", and show what was saved; if it failed, say it was not saved and why.')
OWN_HINT = ('[kal] If the user just told you something that will matter in later conversations (their preferences, '
            'decisions, plans, people, facts about them or their work), save it with kal_note in their own words, then '
            'answer, and add one line saying what you kept. If it is small talk or only a question, do not save.')
SAVED_CLAIM = re.compile(alt('saved_claim') + '|'
                         r'\b(saved|noted|stored|i will remember|i\'ll remember|got it down)\b', re.I)


def before_task(task, ctx):
    o = opts(ctx['cfg']); ctx['kal_task'] = task; out = []
    ctx['kal_searched'] = False; ctx['kal_hits'] = 0; ctx['kal_injected'] = 0; ctx['kal_seen'] = []
    if kchat.wants_note(task): out.append(NOTE_HINT)
    elif o['chat_write'] and kchat.tells(task): out.append(OWN_HINT)
    if o['inject']:
        t0 = time.time(); s = context(ctx['cfg'], task)
        ctx['kal_injected'] = len(s); ctx['kal_secs'] = round(time.time() - t0, 2)
        if s: out.append(s); ctx['kal_seen'].append(s)
    return '\n\n'.join(out) or None


LIB_CLAIM = re.compile(r'(kal|' + alt('lib_word') + r'|library|notes?\b)[^\u3002\uff01\uff1f\n.!?]{0,16}?'
                       r'(' + alt('lib_verb') + r'|remembers?|apart from|except|says?|shows?|has|have|contains?|found|saved|recorded|records?)', re.I)
LIB_SRC = re.compile(r'(' + alt('lib_from') + r'|from|according to)[^\uff0c,\u3002\n]{0,8}?(kal|' + alt('lib_short') + r'|library|notes?\b)', re.I)   # "according to your kal library"
LIB_PUT = re.compile(r'(' + alt('lib_put') + r'|saved (?:it )?(?:to|in)|stored (?:it )?in|added (?:it )?to)'
                     r'[^\uff0c,\u3002\n]{0,8}?(kal|' + alt('lib_short') + r'|library|notes?\b)', re.I)    # "I have saved it to kal"
LIB_NEG = re.compile(alt('negation') + r'|\b(nothing|no|not|none|empty)\b|n\'t', re.I)


QUOTED = re.compile(r'\u300c([^\u300d]{2,60})\u300d|\u201c([^\u201d]{2,60})\u201d|"([^"\n]{2,60})"|\*\*([^*\n]{2,60})\*\*')


def _sentences(answer):
    """Sentences; a line ending in a colon runs on into what follows ("kal has one:\n- **Chan Bakery**")."""
    a = re.sub(r'[\uff1a:][ \t]*\n+', '\uff1a ', answer or '')
    a = re.sub(r'(\uff1a [^\n]*)\n(?=[ \t]*(?:[-*\u2022]|\d+[.)\u3001]) )', r'\1 ', a)   # keep the list under the colon together
    while True:
        b = re.sub(r'(\uff1a [^\n]*)\n(?=[ \t]*(?:[-*\u2022]|\d+[.)\u3001]) )', r'\1 ', a)
        if b == a: break
        a = b
    return re.split(r'[\u3002\uff01\uff1f!?]|\n', a)


def _claim_in(sentence):
    """A sentence says the library holds something. Quoted words are not the answer's own claim ("I said 'kal has it'")."""
    bare = QUOTED.sub('\u300c\u300d', sentence)
    for m in list(LIB_CLAIM.finditer(bare)) + list(LIB_SRC.finditer(bare)):
        tail = re.split(r'[\uff0c,;\uff1b]', bare[m.end():m.end() + 10])[0]   # "has nothing", "not recorded"
        if not LIB_NEG.search(m.group(0) + tail): return True
    for m in LIB_PUT.finditer(bare):
        if re.search(r'(' + alt('offer') + r'|will|can|could|want)', bare[max(0, m.start() - 8):m.start()]): continue   # an offer, not a claim
        if not LIB_NEG.search(bare[max(0, m.start() - 4):m.end()]): return True
    return False


def claims_library(answer):
    """The answer says the user's library holds something (not 'the library has nothing')."""
    return any(_claim_in(x) for x in _sentences(answer))


def _grams(t):
    t = re.sub(r'\s+', '', t.lower()); return {t[i:i + 3] for i in range(len(t) - 2)} or ({t} if t else set())


def unsupported(answer, seen):
    """What the answer credits to the library but the library did not return this turn. None = supported.
    Per sentence that claims the library has something: each quote in it (unless the quote is negated: "no 'X'")
    must be in what the library returned; a claim without quotes needs at least some library material."""
    ev = re.sub(r'\s+', '', '\n'.join(seen).lower())
    for sent in _sentences(answer):
        if not _claim_in(sent): continue
        quotes = [(m.start(), next(g for g in m.groups() if g)) for m in QUOTED.finditer(sent)]
        quotes = [q for i, q in quotes if not LIB_NEG.search(sent[max(0, i - 4):i])]
        if not quotes and not ev: return '(anything)'
        for q in quotes:
            g = _grams(q)
            if sum(x in ev for x in g) / len(g) < 0.5: return q
    return None


REWRITE = (' Do not call kal_note now.'
           ' Write the whole answer again for the user, as if for the first time: do not mention this check, a correction or an earlier version.')


def before_delivery(answer, ctx):
    """Two checks: (1) the user asked to keep something and the answer says it is saved: it must really be saved.
    (2) the answer says the library holds something: what it quotes must be in what the library returned this turn
    (kal_search / kal_read / kal_note / injected [kal] material). Otherwise it came from the conversation, not the library."""
    if kchat.wants_note(ctx.get('kal_task')) and not ctx.get('kal_noted') and SAVED_CLAIM.search(answer or ''):
        return ('kal: your answer says it was saved, but kal_note was not called or did not succeed, so nothing is saved yet. '
                'Call kal_note with the user\'s own words now; or tell the user plainly that it was not saved.')
    if claims_library(answer):
        miss = unsupported(answer, ctx.get('kal_seen') or [])
        if miss is None: return None
        what = '' if miss == '(anything)' else f' ("{miss}")'
        if not kchat.wants_note(ctx.get('kal_task')): ctx['kal_no_note'] = True   # a source fix must not turn into a save
        if not ctx.get('kal_searched'):
            return (f'kal: your answer says the library has this{what}, but you did not look in the library this turn. '
                    'Call kal_search to check. If it is not there and you know it from earlier in this conversation, say it '
                    'comes from this conversation, not from kal.' + REWRITE)
        return (f'kal: your answer says the library has this{what}, but kal_search did not return it this turn, so the library '
                'does not have it. If you know it from earlier in this conversation, say it comes from this conversation, not '
                'from kal (it is not in the library, so it will be gone after this session; the user can say "note this" to keep it). '
                'If you do not know it, say so.' + REWRITE)
    return None


SEARCH = {'type': 'function', 'function': {'name': 'kal_search',
          'description': "Search the user's own library (documents they added to kal, and earlier conversations). Returns refs with short previews.",
          'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'k': {'type': 'integer', 'description': 'max results (default 5)'}},
                         'required': ['query']}}}
NOTE = {'type': 'function', 'function': {'name': 'kal_note',
        'description': "Save something to the user's own library so later conversations can find it: (1) what the user asked "
                       "you to keep (\"note this\"); (2) on your own judgement, what the user told you that will matter "
                       "again: their preferences, decisions, plans, facts about them, their people or their work, names and "
                       "numbers they will need. Not small talk, not your own answers or guesses, not what the library already has. "
                       "Use the user's own words; never add or guess details. If you save on your own, tell the user in one line "
                       "what you kept. Returns what was really saved.",
        'parameters': {'type': 'object', 'properties': {'text': {'type': 'string', 'description': "the user's own words to keep"}},
                       'required': ['text']}}}
READ = {'type': 'function', 'function': {'name': 'kal_read',
        'description': 'Show the full text of one library passage by its ref (from kal_search or from the [kal] material).',
        'parameters': {'type': 'object', 'properties': {'ref': {'type': 'string'}}, 'required': ['ref']}}}


def kal_search(ctx, query, k=5):
    lines = []
    for lib, c in _libs(ctx['cfg']):
        hits = store.search(c, query, k=int(k or 5), min_rel=0.05)
        ctx.setdefault('kal_seen', []).extend(x['text'] for x in hits)
        lines += [f"[{x['ref']}] (rel {x['rel']}): {x['text'][:200].replace(chr(10), ' ')}" for x in hits]
        c.close()
    ctx['kal_searched'] = True; ctx['kal_hits'] = ctx.get('kal_hits', 0) + len(lines)
    return True, '\n'.join(lines) or 'nothing in the library matches; try other words.'


def kal_read(ctx, ref):
    for lib, c in _libs(ctx['cfg']):
        r = store.read_ref(c, ref.strip().strip('[]')); c.close()
        if r:
            ctx['kal_hits'] = ctx.get('kal_hits', 0) + 1; ctx.setdefault('kal_seen', []).append(r['text'])
            return True, f"[{r['ref']}]\n{r['text']}" + (f"\n(neighbours: {', '.join(r['neighbours'])})" if r['neighbours'] else '')
    return False, f'no passage with ref {ref!r}; use kal_search first.'


NOTE_GROUND = 0.6     # own-choice notes: share of 3-grams that must come from what the user said


def _user_said(ctx):
    msgs = [m.get('content') for m in ctx.get('messages') or [] if m.get('role') == 'user' and isinstance(m.get('content'), str)]
    return ''.join(x.split('\n\n[kal]')[0] for x in msgs) + (ctx.get('kal_task') or '')


def kal_note(ctx, text):
    text = (text or '').strip()
    if not text: return False, 'kal_note: nothing to save (text is empty). Nothing was saved.'
    asked = kchat.wants_note(ctx.get('kal_task'))
    if ctx.get('kal_no_note') and not asked:
        return False, ('kal_note: not saved. You are fixing where a fact came from; saving it now would make the claim look '
                       'true. Answer the question and say where your facts came from.')
    if not asked:
        if not opts(ctx['cfg'])['chat_write']:
            return False, 'kal_note: not saved. The conversation library is off; only what the user asks to keep ("note this") is saved.'
        g = _grams(text); said = re.sub(r'\s+', '', _user_said(ctx).lower())
        if sum(x in said for x in g) / len(g) < NOTE_GROUND:
            return False, ('kal_note: not saved. On your own you may only keep what the user said, in their words; this text '
                           'is not in what the user said. Use the user\'s own words, or do not save.')
    try:
        ref, stored = kchat.save_note(ctx['cfg'], ctx.get('session') or '', ctx.get('kal_task') or text, text,
                                      kind='note' if asked else 'auto')
    except Exception as e:
        return False, f'kal_note failed: {type(e).__name__}: {e}. Nothing was saved; tell the user.'
    if not ref: return False, 'kal_note: nothing was saved (empty after masking). Tell the user.'
    ctx['kal_noted'] = True; ctx.setdefault('kal_seen', []).append(stored)
    masked = ' (some never-remember content was masked)' if stored != text else ''
    tail = '' if asked else '\n(saved on your own choice: tell the user in one line what you kept, so they can say if they do not want it.)'
    return True, f'{kchat.NOTED} [{ref}]{masked}:\n{stored}{tail}'


def after_delivery(answer, ctx, checks):
    """Background (the answer is already delivered): save what the user said and what tools returned. No model call."""
    cfg = ctx['cfg']
    task = ctx.get('task') or ''
    if not opts(cfg)['chat_write'] and not kchat.wants_note(task): return None
    out = kchat.learn_exchange(cfg, task, ctx.get('messages') or [], ctx.get('tools_used') or 0, ctx.get('session') or '',
                               noted=bool(ctx.get('kal_noted')))
    ctx['kal_chat'] = out
    try:
        from lamellar.core.events import emit; emit('kal', **out)
    except Exception: pass
    return None


def _chat_libs(cfg):
    d = os.path.dirname(store.lib_path(cfg))
    return sorted(f[:-3] for f in os.listdir(d) if f.startswith('chat-') and f.endswith('.db'))


USAGE = """usage: lamellar kal add <folder> [--lib NAME] | search "<query>" | read <ref> | status | context "<task>"
       lamellar kal chats | rebuild --lib chat-...   (conversation libraries)
Libraries are SQLite files in ~/.lamellar/kal/; "libs" in the kal config says which ones answer questions."""


def command(args, cfg):
    a = list(args); cmd = a.pop(0) if a else 'status'
    def flag(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1] if i + 1 < len(a) else default; del a[i:i + 2]; return v
        return default
    lib = flag('--lib', 'default')
    if cmd == 'add':
        if not a: print(USAGE); return 1
        c = store.connect(store.lib_path(cfg, lib)); t0 = time.time()
        st = store.add_folder(c, a[0]); s = store.status(c); c.close()
        print(f"{lib}: {st['added']} new, {st['changed']} changed, {st['skipped']} unchanged, {st['chunks']} passages ({time.time() - t0:.1f}s). "
              "searchable now.")
        if lib not in opts(cfg)['libs']: print(f'note: "{lib}" is not in kal.libs in worker.json, so it will not be used when answering.')
        return 0
    if cmd == 'search':
        ok, out = kal_search({'cfg': cfg}, ' '.join(a)); print(out); return 0
    if cmd == 'read':
        ok, out = kal_read({'cfg': cfg}, ' '.join(a)); print(out); return 0 if ok else 1
    if cmd == 'context':
        s = context(cfg, ' '.join(a)); print(s or '(nothing relevant: no text would be added)'); print(f'--- {len(s)} characters'); return 0
    if cmd == 'status':
        p = store.lib_path(cfg, lib)
        if not os.path.isfile(p): print(f'{lib}: empty. Add a folder: lamellar kal add <folder>'); return 0
        c = store.connect(p); s = store.status(c); c.close()
        print(f"{lib}: {s['files']} files, {s['chunks']} passages")
        return 0
    if cmd == 'chats':
        for l in _chat_libs(cfg):
            c = store.connect(store.lib_path(cfg, l)); s = store.status(c); c.close()
            print(f"{l}: {s['files']} conversations, {s['chunks']} passages")
        return 0
    if cmd == 'rebuild':
        if not lib.startswith('chat-'): print('rebuild is for conversation libraries (--lib chat-...); folder libraries: add the folder again'); return 1
        print(kchat.rebuild(cfg, lib)); return 0
    print(USAGE); return 0 if cmd in ('help', '-h', '--help') else 1


def register(api):
    api.add_tool(SEARCH, kal_search); api.add_tool(READ, kal_read); api.add_tool(NOTE, kal_note)
    api.on('before_task', before_task); api.on('before_delivery', before_delivery); api.on('after_delivery', after_delivery)
    api.add_command('kal', command, 'local knowledge library')
