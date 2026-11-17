"""kal: knowledge armor. Your folders become a local library the model can search. Fully offline, standard library only.

Config (worker.json):
  "kal": {"libs": ["default"], "max_chars": 3000, "min_rel": 0.05, "rel_top": 0.5, "k": 5, "inject": true,
          "rerank_model": "", "rerank_keep": 5, "timeout_s": 30, "chat_write": false, "chat_rerank": true, "chat_rerank_keep": 3}
  chat_write: after each delivered answer that used tools or stated facts, what the user said and what tools returned is
  saved to the user's private library (chat-<bot>-<chat>.db, see kalcore/chat.py), searched only for the same user.
  chat_rerank: conversation passages are long tool outputs, so word overlap alone lets unrelated questions in (6 of 10 in KL9);
  each candidate is scored by the model (rerank_model, else the main model) and kept at >= chat_rerank_keep (0 of 10 in KL9).
Notes digested by a model were removed 11-17: they added nothing over the original passages (KL4) and invented details (KL9).
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path: sys.path.insert(0, HERE)
from kalcore import store, chat as kchat

DEFAULTS = {'libs': ['default'], 'max_chars': 3000, 'min_rel': 0.05, 'rel_top': 0.5, 'k': 5, 'inject': True,
            'rerank_model': '', 'rerank_keep': 5, 'timeout_s': 30, 'chat_write': False, 'chat_rerank': True, 'chat_rerank_keep': 3}


def opts(cfg): return {**DEFAULTS, **(cfg.get('kal') or {})}


def _libs(cfg, user=True):
    """The configured libraries, plus this user's own conversation library (never another user's)."""
    names = [l for l in opts(cfg)['libs'] if not l.startswith('chat-')]
    if user and opts(cfg)['chat_write']: names.append(kchat.lib_name(cfg))
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
            if room > 200: parts.append(f"\n[{x['ref']}]\n{x['text'][:room]}…")
            break
        parts.append(s); used += len(s)
    return ''.join(parts) if len(parts) > 1 else ''


def before_task(task, ctx):
    o = opts(ctx['cfg'])
    if not o['inject']: return None
    t0 = time.time(); s = context(ctx['cfg'], task)
    ctx['kal_injected'] = len(s); ctx['kal_secs'] = round(time.time() - t0, 2)
    return s or None


SEARCH = {'type': 'function', 'function': {'name': 'kal_search',
          'description': "Search the user's own library (documents they added to kal, and earlier conversations). Returns refs with short previews.",
          'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'k': {'type': 'integer', 'description': 'max results (default 5)'}},
                         'required': ['query']}}}
READ = {'type': 'function', 'function': {'name': 'kal_read',
        'description': 'Show the full text of one library passage by its ref (from kal_search or from the [kal] material).',
        'parameters': {'type': 'object', 'properties': {'ref': {'type': 'string'}}, 'required': ['ref']}}}


def kal_search(ctx, query, k=5):
    lines = []
    for lib, c in _libs(ctx['cfg']):
        lines += [f"[{x['ref']}] (rel {x['rel']}): {x['text'][:200].replace(chr(10), ' ')}" for x in store.search(c, query, k=int(k or 5), min_rel=0.05)]
        c.close()
    return True, '\n'.join(lines) or 'nothing in the library matches; try other words.'


def kal_read(ctx, ref):
    for lib, c in _libs(ctx['cfg']):
        r = store.read_ref(c, ref.strip().strip('[]')); c.close()
        if r: return True, f"[{r['ref']}]\n{r['text']}" + (f"\n(neighbours: {', '.join(r['neighbours'])})" if r['neighbours'] else '')
    return False, f'no passage with ref {ref!r}; use kal_search first.'


def after_delivery(answer, ctx, checks):
    """Background (the answer is already delivered): save what the user said and what tools returned. No model call."""
    cfg = ctx['cfg']
    if not opts(cfg)['chat_write']: return None
    out = kchat.learn_exchange(cfg, ctx.get('task') or '', ctx.get('messages') or [], ctx.get('tools_used') or 0, ctx.get('session') or '')
    ctx['kal_chat'] = out
    try:
        from lamellar.core.events import emit; emit('kal', **out)
    except Exception: pass
    return None


def _chat_libs(cfg):
    d = os.path.dirname(store.lib_path(cfg))
    return sorted(f[:-3] for f in os.listdir(d) if f.startswith('chat-') and f.endswith('.db'))


USAGE = """usage: lamellar kal add <folder> [--lib NAME] | search "<query>" | read <ref> | status | context "<task>"
       lamellar kal chats | rebuild --lib chat-…   (conversation libraries)
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
        if not lib.startswith('chat-'): print('rebuild is for conversation libraries (--lib chat-…); folder libraries: add the folder again'); return 1
        print(kchat.rebuild(cfg, lib)); return 0
    print(USAGE); return 0 if cmd in ('help', '-h', '--help') else 1


def register(api):
    api.add_tool(SEARCH, kal_search); api.add_tool(READ, kal_read)
    api.on('before_task', before_task); api.on('after_delivery', after_delivery)
    api.add_command('kal', command, 'local knowledge library')
