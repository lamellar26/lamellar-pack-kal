"""kal: knowledge armor. Your folders become a local library the model can search; at night a local model digests it into
short notes that quote the text word for word (checked by a program). Fully offline, standard library only.

Config (worker.json):
  "kal": {"libs": ["default"], "max_chars": 3000, "min_rel": 0.05, "rel_top": 0.5, "k": 5, "inject": true, "use_nodes": true,
          "rerank_model": "", "rerank_keep": 5, "digest_model": "", "night_minutes": 120, "timeout_s": 30,
          "chat_write": false, "chat_learn": true, "chat_nodes": false, "chat_model": "", "chat_timeout_s": 120}
  chat_write: after each delivered answer that used tools or stated facts, what the user said and what tools returned is
  saved to the user's private library (chat-<bot>-<chat>.db, see kalcore/chat.py), searched only for the same user.
  chat_nodes: also have the model write notes from it. Off by default: in the KL9 replay of 20 real conversations
  a 27B writer invented details in 3 of 24 notes even after the program QC.
"""
import json, os, sys, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path: sys.path.insert(0, HERE)
from kalcore import store, digest, chat as kchat

DEFAULTS = {'libs': ['default'], 'max_chars': 3000, 'min_rel': 0.05, 'rel_top': 0.5, 'k': 5, 'inject': True, 'use_nodes': True,
            'rerank_model': '', 'rerank_keep': 5, 'digest_model': '', 'night_minutes': 120, 'timeout_s': 30, 'review_model': '',
            'chat_write': False, 'chat_learn': True, 'chat_nodes': False, 'chat_model': '', 'chat_timeout_s': 120}


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
        txt = cd.get('text') or (cd.get('title', '') + '\n' + cd.get('body', ''))
        try:
            out = chat([{'role': 'user', 'content': f'Question:\n{task[:1500]}\n\nPassage:\n{txt[:1500]}\n\n'
                         'How useful is the passage for answering the question? Reply with one number 0-10 only.'}])
            n = int(''.join(ch for ch in out if ch.isdigit())[:2] or 0)
        except Exception: n = 0
        if n >= o['rerank_keep']: keep.append(cd)
    return keep


def context(cfg, task, use_nodes=None):
    """The text added to the task: notes first, then the original passages they cite. Empty string if nothing is relevant."""
    return _context(cfg, task, use_nodes)[0]


def _context(cfg, task, use_nodes=None):
    """(text, shown notes). Conversation notes also bring associated notes up to two links away."""
    o = opts(cfg); use_nodes = o['use_nodes'] if use_nodes is None else use_nodes
    deadline = time.time() + o['timeout_s']; nodes, chunks = [], []
    for lib, c in _libs(cfg):
        r = store.search(c, task, k=o['k'], min_rel=o['min_rel'], nodes=use_nodes, rel_top=o['rel_top'])
        found = r['nodes'] + (kchat.hops(c, r['nodes'], min_rel=o['min_rel']) if use_nodes and lib.startswith('chat-') else [])
        nodes += [dict(n, lib=lib) for n in found]; chunks += [dict(x, lib=lib) for x in r['chunks']]
        if use_nodes:   # the passages a note cites come along, even if they did not match by themselves
            have = {x['ref'] for x in chunks}
            for n in found:
                for ref in n['refs']:
                    if ref in have: continue
                    rr = store.read_ref(c, ref)
                    if rr: chunks.append({'ref': ref, 'text': rr['text'], 'rel': n['rel'], 'lib': lib}); have.add(ref)
        c.close()
    nodes.sort(key=lambda x: -x['rel']); chunks.sort(key=lambda x: -x['rel'])
    if o['rerank_model'] and (nodes or chunks):
        nodes, chunks = _rerank(cfg, o, task, nodes, deadline), _rerank(cfg, o, task, chunks, deadline)
    if not nodes and not chunks: return '', []
    parts, used = [], 0
    head = ('[kal] Material from the user\'s own library that may help. Use it only if it is relevant; cite the [ref] when you use it. '
            'kal_read(ref) shows a passage in full, kal_search(query) searches the library.')
    def put(s):
        nonlocal used
        if used + len(s) > o['max_chars']: return False
        parts.append(s); used += len(s); return True
    put(head); shown = []
    for n in nodes:
        if n.get('origin') == 'chat':
            tag = f" [{n['id']}] (from an earlier conversation" + (', not yet confirmed' if n.get('trust') == 'low' else '') + ')'
        else: tag = ''
        if not put(f"\n- {n['title']}{tag}: {n['body']} [refs: {', '.join(n['refs'])}]"): break
        shown.append(n)
    for x in chunks:
        if not put(f"\n[{x['ref']}]\n{x['text']}"):
            room = o['max_chars'] - used - len(x['ref']) - 30
            if room > 200: put(f"\n[{x['ref']}]\n{x['text'][:room]}…")
            break
    return (''.join(parts), shown) if len(parts) > 1 else ('', [])


def before_task(task, ctx):
    o = opts(ctx['cfg'])
    if not o['inject']: return None
    t0 = time.time(); s, shown = _context(ctx['cfg'], task)
    ctx['kal_injected'] = len(s); ctx['kal_secs'] = round(time.time() - t0, 2); ctx['kal_shown'] = shown
    return s or None


SEARCH = {'type': 'function', 'function': {'name': 'kal_search',
          'description': "Search the user's own library (documents they added to kal, and notes digested from them). Returns refs with short previews.",
          'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'k': {'type': 'integer', 'description': 'max results (default 5)'}},
                         'required': ['query']}}}
READ = {'type': 'function', 'function': {'name': 'kal_read',
        'description': 'Show the full text of one library passage or note by its ref (from kal_search or from the [kal] material).',
        'parameters': {'type': 'object', 'properties': {'ref': {'type': 'string'}}, 'required': ['ref']}}}


def kal_search(ctx, query, k=5):
    lines = []
    for lib, c in _libs(ctx['cfg']):
        r = store.search(c, query, k=int(k or 5), min_rel=0.05, nodes=opts(ctx['cfg'])['use_nodes'])
        lines += [f"note {n['id']} (rel {n['rel']}): {n['title']}: {n['body'][:150]} [refs: {', '.join(n['refs'])}]" for n in r['nodes']]
        lines += [f"[{x['ref']}] (rel {x['rel']}): {x['text'][:200].replace(chr(10), ' ')}" for x in r['chunks']]
        c.close()
    return True, '\n'.join(lines) or 'nothing in the library matches; try other words.'


def kal_read(ctx, ref):
    for lib, c in _libs(ctx['cfg']):
        r = store.read_ref(c, ref.strip().strip('[]')); c.close()
        if r: return True, f"[{r['ref']}]\n{r['text']}" + (f"\n(neighbours: {', '.join(r['neighbours'])})" if r['neighbours'] else '')
    return False, f'no passage or note with ref {ref!r}; use kal_search first.'


def _busy(cfg):
    """True if the lamellar entry says a user job is running (then the nightly digest yields)."""
    try:
        with open(os.path.join(os.path.expanduser(cfg.get('state_dir') or '~/.lamellar'), 'entry.json')) as f: port = json.load(f).get('port', 8102)
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as r: return bool(json.load(r).get('busy'))
    except Exception: return False


def learn(cfg, minutes=None, model_name=None, log=print):
    o = opts(cfg); deadline = time.time() + 60 * float(minutes or o['night_minutes'])
    chat = _chat(cfg, model_name or o['digest_model'])
    review = None
    if o['review_model']:
        rchat = _chat(cfg, o['review_model'], max_tokens=8, timeout=60)
        def review(n, texts):
            src = '\n'.join(texts[r] for r in n['refs'])
            out = rchat([{'role': 'user', 'content': f'Text:\n{src}\n\nNote: {n["title"]}: {n["body"]}\n\nDoes the note say anything the text does not say? Reply yes or no.'}])
            return 'review: note claims something not in the text' if 'yes' in out.lower() else None
    total = {}
    for lib, c in _libs(cfg):
        st = digest.learn(c, chat, deadline=deadline, busy=lambda: _busy(cfg), review=review, log=lambda m: log(f'[{lib}] {m}'))
        c.close(); total[lib] = st
        if st['stopped']: break
    return total


def after_delivery(answer, ctx, checks):
    """Background (the answer is already delivered): strengthen cited conversation notes, then write new ones."""
    cfg = ctx['cfg']; o = opts(cfg)
    if not o['chat_write'] or not o['chat_learn']: return None     # chat_learn false = read the conversation library, write nothing
    lib = kchat.lib_name(cfg); out = {}
    shown = [n for n in ctx.get('kal_shown') or [] if n.get('lib') == lib]
    if shown: out['strengthened'] = kchat.reinforce(cfg, lib, shown, answer)
    chat = _chat(cfg, o['chat_model'], max_tokens=2000, timeout=o['chat_timeout_s'])
    out.update(kchat.learn_exchange(cfg, chat, ctx.get('task') or '', answer, ctx.get('messages') or [], ctx.get('tools_used') or 0,
                                    ctx.get('session') or '', log=lambda m: print('[kal chat]', m, file=sys.stderr), nodes=o['chat_nodes']))
    ctx['kal_chat'] = out
    try:
        from lamellar.core.events import emit; emit('kal', **out)
    except Exception: pass
    return None


def _chat_libs(cfg):
    d = os.path.dirname(store.lib_path(cfg))
    return sorted(f[:-3] for f in os.listdir(d) if f.startswith('chat-') and f.endswith('.db'))


def nightly(ctx):
    learn(ctx['cfg'], log=lambda m: print('[kal]', m, file=sys.stderr))
    for lib in _chat_libs(ctx['cfg']): kchat.decay(ctx['cfg'], lib)


USAGE = """usage: lamellar kal add <folder> [--lib NAME] | search "<query>" | read <ref> | status | learn [--minutes N] [--model M] | context "<task>"
       lamellar kal chats | confirm <note id> --lib chat-… | rebuild --lib chat-…   (conversation libraries)
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
              f"Searchable now; {s['windows_pending']} window(s) waiting for the nightly digest.")
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
        print(f"{lib}: {s['files']} files, {s['chunks']} passages; digest {s['windows_done']}/{s['windows']} windows done "
              f"({s['windows_pending']} pending, {s['windows_failed']} failed); {s['nodes']} notes; "
              f"QC pass {s['qc_pass']}, rejected {s['qc_reject']}" + (f" ({s['qc_rate']:.0%})" if s['qc_rate'] is not None else '')
              + (f"; last learn {s['last_learn']}" if s['last_learn'] else ''))
        return 0
    if cmd == 'chats':
        for l in _chat_libs(cfg):
            c = store.connect(store.lib_path(cfg, l)); g = lambda q: c.execute(q).fetchone()[0]
            ok_n = g("SELECT count(*) FROM node WHERE status='ok'"); low = g("SELECT count(*) FROM node WHERE status='ok' AND trust='low'")
            sup = g("SELECT count(*) FROM node WHERE status='superseded'"); conv = g('SELECT count(*) FROM source'); c.close()
            print(f'{l}: {ok_n} notes ({low} not yet confirmed), {sup} superseded, {conv} conversations')
        return 0
    if cmd == 'confirm':
        if not a or not lib.startswith('chat-'): print(USAGE); return 1
        ok = kchat.set_trust(cfg, lib, a[0]); print('confirmed' if ok else 'no such note'); return 0 if ok else 1
    if cmd == 'rebuild':
        if not lib.startswith('chat-'): print('rebuild is for conversation libraries (--lib chat-…); folder libraries: add the folder again'); return 1
        print(json.dumps(kchat.rebuild(cfg, lib))); return 0
    if cmd == 'learn':
        m = flag('--minutes'); model_name = flag('--model')
        print(json.dumps(learn(cfg, m, model_name), ensure_ascii=False)); return 0
    print(USAGE); return 0 if cmd in ('help', '-h', '--help') else 1


def register(api):
    api.add_tool(SEARCH, kal_search); api.add_tool(READ, kal_read)
    api.on('before_task', before_task); api.on('after_delivery', after_delivery); api.on('nightly', nightly)
    api.add_command('kal', command, 'local knowledge library')
