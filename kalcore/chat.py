"""Conversation -> the user's private KAL library (KL6-KL8).

After the answer is delivered (the user does not wait), facts that came up in the exchange are written as notes:
  - only when there is something factual: a tool was used, or the user stated facts (pure chat never calls a model)
  - three routes: preferences / how to work -> core memory (unchanged), "from now on..." -> correction capture (unchanged),
    facts / knowledge -> here. At most MAX_ITEMS per exchange.
  - the source is what the USER said and what TOOLS returned, saved as text; the assistant's own words are never a source
    (a note can not prove itself by quoting the model's answer)
  - before writing, the nearest existing notes are shown to the model: add / update (old note superseded, never deleted) / skip
  - program QC as in the nightly digest, plus the never-remember list; a failed item is sent back at most twice, then dropped
  - one library per user (chat-<bot>-<chat>.db); users can not see each other's
Markdown is the source of truth: <state>/kal/<lib>/sources/*.md and nodes/*.md; `lamellar kal rebuild --lib <lib>` rebuilds the db.
Brain mechanics (lessons from the Sept 2026 winner-takes-all bug): notes start weak (trust low) and fade unless used;
being found does not strengthen a note, only being cited in an answer does; only direct hits are strengthened; edge weight <= MAX_W;
edges decay every night; trust rises to normal after two more cited uses or `lamellar kal confirm <id>`.
"""
import hashlib, json, os, re, time
from . import store, digest

MAX_ITEMS = 3
MAX_W = 3.0
EDGE_DECAY = 0.9
PROMOTE_AT = 3          # n_used starts at 1; cited twice more -> trust normal
SRC_CAP, SRC_TOTAL = 4000, 12000
MARK = '<<<kal-ref {}>>>'

PROMPT = """You keep a private notebook of FACTS for an assistant, so that in later conversations it knows them without being told again.

Below are passages from one exchange: what the USER wrote (labels ending #u…) and what TOOLS returned (labels ending #t…).
The assistant's final answer is shown only so you understand what happened. It is NOT a source: never quote it.

Worth a note: a result of a method, test or experiment; what a file or document contains; a business fact (names, products, prices, dates, where something is); a decision the user stated.
NOT worth a note (another system handles these, or they do not last): the user's preferences or how they want work done; corrections ("from now on…"); greetings or chit-chat; what the assistant did; anything an existing note below already says; passwords, ID numbers, account or phone numbers, emails, specific money amounts.

Existing notes that may overlap (id: title: body):
{existing}

Rules for every note (a program checks; a note that breaks one is thrown away):
- "quotes": 1-3 sentences copied EXACTLY, character for character, from the passages.
- "refs": the labels of the passages the quotes come from (only labels shown below).
- "terms": 2-6 words or names that appear literally in those passages.
- "title" short and specific; "body" 1-3 sentences, at most 150 characters, same language as the passages; every number or name in it must appear in the passages.
- "op": "add" for a new fact; "update" when it corrects or replaces an existing note (put that note's id in "replaces").
- "links": ids of existing notes that are about the same thing (may be empty).

This exchange already passed a filter: it used tools or the user stated facts. If the passages hold durable facts (numbers, names,
versions, paths, settings, measured results, what a file says), write 1 to {max_items} notes about the most useful ones.
Reply {{"items": []}} only if there is truly nothing factual worth keeping.
Reply with JSON only:
{{"items": [{{"op": "add", "replaces": "", "title": "...", "body": "...", "quotes": ["..."], "refs": ["..."], "terms": ["..."], "links": []}}]}}

Passages:
{passages}

Assistant's final answer (context only, NOT a source):
{answer}"""

FIX = """Some items failed the program check. Fix ONLY these items (or drop them) and reply with the same JSON format:
{problems}"""


# ---------- who / where ----------
def user_key(cfg):
    """Same scope as core memory: bot + chat. Each user gets their own library."""
    raw = f"{cfg.get('armor_bot') or 'default'}-{cfg.get('armor_chat') or 'default'}"
    return re.sub(r'[^A-Za-z0-9._-]', '_', raw)[:58]


def lib_name(cfg): return 'chat-' + user_key(cfg)


def md_dir(cfg, lib):
    return os.path.join(os.path.expanduser(cfg.get('state_dir') or '~/.lamellar'), 'kal', lib)


# ---------- KL6: should we look at all, and what is the source ----------
GREETING = re.compile(r'^\s*(hi|hello|hey|thanks?|thank you|ok|okay|好|好的|多謝|唔該|謝謝|你好|早晨|在嗎|喺度嗎|bye|拜拜)[\s!！.。?？~]*$', re.I)
QUESTION_END = re.compile(r'([?？]|嗎|吗|呢|未|冇|咩|乜|邊個|點樣|點解|幾多|什麼|甚麼|怎樣|如何)\s*[?？!！。.]*\s*$')
FACTY = re.compile(r'\d|[A-Za-z0-9_\-]+\.[A-Za-z]{2,4}\b|https?://|「.+?」|"[^"]{2,}"|[A-Z][a-z]+[A-Z]?|[A-Z]{2,}')


def stated_facts(text):
    """Sentences in the user's message that state something (not only ask), and carry something concrete."""
    out = []
    for s in re.split(r'(?<=[。！!；;\n])|(?<=[?？])', text or ''):
        s = s.strip()
        if len(s) < 8 or GREETING.match(s) or QUESTION_END.search(s): continue
        if FACTY.search(s) or len(s) >= 25: out.append(s)
    return out


def should_write(task, tools_used, tool_texts):
    """KL6 gate: pure chat never calls the model."""
    if GREETING.match(task or ''): return False
    if tools_used and any(t.strip() for t in tool_texts): return True
    return bool(stated_facts(task))


def this_turn(task, messages):
    """The tool results of this turn only (messages also hold earlier turns of the session)."""
    start = 0
    for i, m in enumerate(messages):
        if m.get('role') == 'user' and isinstance(m.get('content'), str) and task and task[:200] in m['content']: start = i
    return [m.get('content') or '' for m in messages[start:] if m.get('role') == 'tool']


def _redact(text, deny):
    for pat, what in deny:
        text = re.sub(pat, f'[{what} removed]', text)
    return text


def deny_list(cfg):
    try:
        from lamellar.memory import DENY
    except Exception: DENY = []
    return list(DENY) + [(p, 'configured never-remember content') for p in cfg.get('memory_deny') or []]


def save_source(c, cfg, lib, session, task, tool_texts, now=None):
    """Store what the user said and what tools returned as a library source (searchable at once) + a markdown file.
    Never-remember content is masked before it is stored. Returns {ref: text}."""
    now = int(now or time.time()); deny = deny_list(cfg)
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    rel = f"chat/{stamp}-{hashlib.sha1((session + task + str(time.time_ns())).encode()).hexdigest()[:6]}"
    texts = {}
    for j, part in enumerate(store.split(_redact(task, deny)) or [], 1): texts[f'{rel}#u{j}'] = part
    used = 0
    for i, t in enumerate(tool_texts, 1):
        t = _redact(t[:SRC_CAP], deny)
        if used >= SRC_TOTAL: break
        t = t[:SRC_TOTAL - used]; used += len(t)
        for j, part in enumerate(store.split(t), 1): texts[f'{rel}#t{i}.{j}'] = part
    if not texts: return {}
    _insert_source(c, rel, session, now, texts)
    d = os.path.join(md_dir(cfg, lib), 'sources'); os.makedirs(d, exist_ok=True)
    body = '\n'.join(MARK.format(r) + '\n' + t for r, t in texts.items())
    _write(os.path.join(d, rel.split('/', 1)[1] + '.md'), f"---\nrel: {rel}\nsession: {session}\ntime: {now}\n---\n{body}\n")
    return texts


def _insert_source(c, rel, session, now, texts):
    sha = hashlib.sha1(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
    sid = c.execute('INSERT INTO source(root, path, rel, sha, added) VALUES (?,?,?,?,?)', ('chat', f'chat:{session}:{rel}', rel, sha, now)).lastrowid
    for i, (ref, t) in enumerate(texts.items(), 1):
        cid = c.execute('INSERT INTO chunk(source_id, ord, ref, text) VALUES (?,?,?,?)', (sid, i, ref, t)).lastrowid
        c.execute('INSERT INTO chunk_fts(rowid, text) VALUES (?,?)', (cid, t))


def _write(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: f.write(text)
    os.replace(tmp, path)


# ---------- KL7: QC + write ----------
PREF = re.compile(r'from now on|next time|going forward|in future|i prefer|i like|i want you|please always|please never|don\'t ever|stop using|'
                  r'以後|下次|今後|唔好再|不要再|別再|我鍾意|我喜歡|我偏好|我想你|記住要|每次都要|一律')


def qc_item(it, texts, answer, deny, candidates):
    """None = pass, else the reason. Builds on the digest QC (quotes word for word in refs, terms and numbers in refs)."""
    if not isinstance(it, dict): return 'not an object'
    if it.get('op') not in ('add', 'update'): return 'op must be add or update'
    if it.get('op') == 'update' and it.get('replaces') not in candidates: return 'replaces must be the id of one of the existing notes shown'
    for q in it.get('quotes') or []:
        if isinstance(q, str) and digest.norm(q) and digest.norm(q) in digest.norm(answer) and not any(digest.norm(q) in digest.norm(t) for t in texts.values()):
            return "quote comes from the assistant's own answer, which is not a source"
    why = digest.qc(it, texts)
    if why: return why
    if PREF.search(' '.join([it.get('title') or '', it.get('body') or ''] + list(it.get('quotes') or [])).lower()):
        return 'this is a preference or a correction (how to work), which belongs to memory, not to a fact note'
    for k in ('title', 'body'):
        for pat, what in deny:
            if re.search(pat, it.get(k) or ''): return f'{k} looks like a {what}, which must not be remembered'
    for q in it.get('quotes') or []:
        for pat, what in deny:
            if re.search(pat, q): return f'quote looks like a {what}, which must not be remembered'
    return None


def nearest(c, query, k=5):
    r = store.search(c, query, k=k, min_rel=0.15, nodes=True, chunks=False)
    return {n['id']: n for n in r['nodes']}


def extract(c, cfg, chat, task, answer, texts, retries=2, log=None):
    """Ask the model for at most MAX_ITEMS notes; program QC; send failures back at most `retries` times.
    Returns (good_items, rejected, candidates)."""
    deny = deny_list(cfg)
    cands = nearest(c, task + '\n' + '\n'.join(list(texts.values())[:6])[:3000])
    existing = '\n'.join(f"{i}: {n['title']}: {n['body']}" for i, n in cands.items()) or '(none)'
    passages = '\n\n'.join(f'[{r}]\n{t}' for r, t in texts.items())
    msgs = [{'role': 'user', 'content': PROMPT.format(existing=existing, passages=passages, answer=(answer or '')[:2000], max_items=MAX_ITEMS)}]
    good, rejected = [], 0
    for attempt in range(retries + 1):
        out = chat(msgs); data = digest.parse_json(out)
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            if attempt == retries: break
            msgs += [{'role': 'assistant', 'content': out}, {'role': 'user', 'content': 'That was not valid JSON. Reply with the JSON object only.'}]
            continue
        problems = []
        for it in items[:MAX_ITEMS - len(good)]:
            why = qc_item(it, texts, answer, deny, cands)
            if why is None: good.append(it)
            else: problems.append((it, why))
        if log: log(f'attempt {attempt + 1}: {len(items)} item(s), {len(problems)} failed QC' + ''.join(f'\n  - {w}' for _, w in problems))
        if not problems or len(good) >= MAX_ITEMS: break
        if attempt == retries: rejected += len(problems); break
        msgs += [{'role': 'assistant', 'content': out},
                 {'role': 'user', 'content': FIX.format(problems='\n'.join(f'- {json.dumps(i, ensure_ascii=False)[:400]}\n  problem: {w}' for i, w in problems))}]
    return good, rejected, cands


def node_md(cfg, lib, c, nid):
    """Write (or rewrite) the markdown file of one note from the db row: md is the source of truth, the db can be rebuilt from it."""
    r = c.execute('SELECT id, title, body, quotes, refs, terms, origin, status, n_used, last_used, created, trust, session, superseded_by FROM node WHERE id=?', (nid,)).fetchone()
    if not r: return
    links = [x[0] for x in c.execute('SELECT dst FROM edge WHERE src=? ORDER BY dst', (nid,))]
    weights = {x[0]: x[1] for x in c.execute('SELECT dst, weight FROM edge WHERE src=?', (nid,))}
    fm = {'id': r[0], 'source': r[6], 'session': r[12], 'created': r[10], 'last_used': r[9], 'n_used': r[8], 'trust': r[11],
          'status': r[7], 'superseded_by': r[13], 'refs': json.loads(r[4] or '[]'), 'quotes': json.loads(r[3] or '[]'),
          'terms': json.loads(r[5] or '[]'), 'links': {k: round(weights[k], 4) for k in links}}
    d = os.path.join(md_dir(cfg, lib), 'nodes'); os.makedirs(d, exist_ok=True)
    head = '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in fm.items())
    body = r[2] + ('\n\n' + ' '.join(f'[[{k}]]' for k in links) if links else '')
    _write(os.path.join(d, nid + '.md'), f"---\n{head}\n---\n# {r[1]}\n\n{body}\n")


def _link(c, a, b, w=1.0):
    if a == b: return
    for s, d in ((a, b), (b, a)):
        c.execute('INSERT INTO edge(src, dst, weight) VALUES (?,?,?) ON CONFLICT(src, dst) DO UPDATE SET weight=min(?, weight+?)', (s, d, min(w, MAX_W), MAX_W, w))


def write(c, cfg, lib, items, cands, session, now=None):
    """Store QC-passed items. update = new note + old one marked superseded (never deleted). Returns new ids."""
    now = int(now or time.time()); ids = []
    for it in items:
        node = {k: it[k] for k in ('title', 'body', 'quotes', 'refs', 'terms') if k in it}
        nid = store.add_node(c, node, origin='chat', trust='low', session=session, now=now)
        if nid in ids: continue                                         # the model repeated the same note
        ids.append(nid)
        touched = {nid}
        if it.get('op') == 'update' and it.get('replaces') in cands:
            c.execute("UPDATE node SET status='superseded', superseded_by=? WHERE id=?", (nid, it['replaces'])); touched.add(it['replaces'])
        for other in it.get('links') or []:
            if other in cands and other != it.get('replaces'): _link(c, nid, other); touched.add(other)
        for t in touched: node_md(cfg, lib, c, t)
    c.commit()
    return ids


def learn_exchange(cfg, chat, task, answer, messages, tools_used=0, session='', log=None, now=None, nodes=True):
    """The whole after-delivery step. Returns a summary dict (never raises into the caller's job; the hook wraps it)."""
    tool_texts = this_turn(task, messages)
    if not should_write(task, tools_used, tool_texts): return {'skipped': 'nothing factual (pure chat)'}
    lib = lib_name(cfg); c = store.connect(store.lib_path(cfg, lib))
    try:
        texts = save_source(c, cfg, lib, session, task, tool_texts, now=now); c.commit()
        if not texts: return {'skipped': 'empty source'}
        if not nodes: return {'lib': lib, 'source_refs': len(texts), 'written': [], 'rejected': 0, 'updated': [], 'mode': 'source only'}
        good, rej, cands = extract(c, cfg, chat, task, answer, texts, log=log)
        ids = write(c, cfg, lib, good, cands, session, now=now)
        return {'lib': lib, 'source_refs': len(texts), 'written': ids, 'rejected': rej,
                'updated': [it['replaces'] for it in good if it.get('op') == 'update']}
    finally: c.close()


# ---------- KL8: use, strengthen, fade, rebuild ----------
def hops(c, hits, max_extra=3, min_rel=0.05):
    """Associated notes up to two links away from direct hits; their score shrinks per hop and with edge weight."""
    have = {h['id'] for h in hits}; extra = []
    frontier = [(h['id'], h['rel']) for h in hits]
    for depth in (1, 2):
        nxt = []
        for nid, rel in frontier:
            for dst, w in c.execute("SELECT e.dst, e.weight FROM edge e JOIN node n ON n.id=e.dst AND n.status='ok' WHERE e.src=? ORDER BY e.weight DESC", (nid,)):
                if dst in have: continue
                r2 = round(rel * 0.5 * min(w, MAX_W) / MAX_W, 3)
                if r2 < min_rel: continue
                row = c.execute('SELECT id, title, body, quotes, refs, origin, trust FROM node WHERE id=?', (dst,)).fetchone()
                extra.append({'id': row[0], 'title': row[1], 'body': row[2], 'quotes': json.loads(row[3] or '[]'), 'refs': json.loads(row[4] or '[]'),
                              'origin': row[5], 'trust': row[6] or 'normal', 'rel': r2, 'hop': depth})
                have.add(dst); nxt.append((dst, r2))
                if len(extra) >= max_extra: return extra
        frontier = nxt
    return extra


def cited(answer, node):
    """Did the answer use this note? Its id, one of its refs, or one of its quotes appears in the answer."""
    a = answer or ''; na = digest.norm(a)
    if node['id'] in a or any(r in a for r in node.get('refs') or []): return True
    return any(len(digest.norm(q)) >= 6 and digest.norm(q) in na for q in node.get('quotes') or [])


def reinforce(cfg, lib, shown, answer, now=None):
    """Hebbian step after an answer: only notes that were direct hits AND cited get n_used+1; edges between co-cited
    direct hits +1 (capped MAX_W). Found-but-not-cited and hop notes get nothing. Returns ids strengthened."""
    hit = [n for n in shown if not n.get('hop') and cited(answer, n)]
    if not hit: return []
    now = int(now or time.time()); c = store.connect(store.lib_path(cfg, lib))
    try:
        for n in hit:
            c.execute('UPDATE node SET n_used=n_used+1, last_used=? WHERE id=?', (now, n['id']))
            c.execute("UPDATE node SET trust='normal' WHERE id=? AND trust='low' AND n_used>=?", (n['id'], PROMOTE_AT))
        for i, a in enumerate(hit):
            for b in hit[i + 1:]: _link(c, a['id'], b['id'])
        c.commit()
        for n in hit: node_md(cfg, lib, c, n['id'])
        for i, a in enumerate(hit):
            for b in hit[i + 1:]: node_md(cfg, lib, c, b['id'])
    finally: c.close()
    return [n['id'] for n in hit]


def decay(cfg, lib):
    """Nightly: every edge weight x EDGE_DECAY (rows kept). Notes fade by themselves through activation()."""
    p = store.lib_path(cfg, lib)
    if not os.path.isfile(p): return 0
    c = store.connect(p)
    try:
        n = c.execute('UPDATE edge SET weight=weight*?', (EDGE_DECAY,)).rowcount; c.commit()
        for (nid,) in c.execute('SELECT DISTINCT src FROM edge').fetchall(): node_md(cfg, lib, c, nid)
        return n
    finally: c.close()


def set_trust(cfg, lib, nid, trust='normal'):
    c = store.connect(store.lib_path(cfg, lib))
    try:
        n = c.execute('UPDATE node SET trust=? WHERE id=?', (trust, nid)).rowcount; c.commit()
        if n: node_md(cfg, lib, c, nid)
        return bool(n)
    finally: c.close()


def _fm(text):
    m = re.match(r'---\n(.*?)\n---\n(.*)', text, re.S)
    if not m: return {}, text
    meta = {}
    for line in m.group(1).split('\n'):
        k, _, v = line.partition(': ')
        try: meta[k] = json.loads(v)
        except ValueError: meta[k] = v
    return meta, m.group(2)


def rebuild(cfg, lib):
    """Rebuild <lib>.db from its markdown. The old db is renamed (kept), never deleted. Returns counts."""
    d = md_dir(cfg, lib); p = store.lib_path(cfg, lib)
    if os.path.isfile(p): os.replace(p, p + time.strftime('.bak-%Y%m%d-%H%M%S'))
    c = store.connect(p); n_src = n_node = 0
    try:
        sdir = os.path.join(d, 'sources')
        for fn in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
            with open(os.path.join(sdir, fn), encoding='utf-8') as f: meta, body = _fm(f.read())
            parts = re.split(r'^<<<kal-ref (.+?)>>>\n', body, flags=re.M)
            texts = {parts[i]: parts[i + 1].rstrip('\n') for i in range(1, len(parts) - 1, 2)}
            _insert_source(c, str(meta.get('rel')), str(meta.get('session') or ''), int(meta.get('time') or 0), texts); n_src += 1
        ndir = os.path.join(d, 'nodes'); edges = []
        for fn in sorted(os.listdir(ndir)) if os.path.isdir(ndir) else []:
            with open(os.path.join(ndir, fn), encoding='utf-8') as f: meta, body = _fm(f.read())
            title = body.split('\n', 1)[0].lstrip('# ').strip()
            text = re.sub(r'\n\n(\[\[[^\]]+\]\] ?)+\s*$', '', body.split('\n', 1)[1].strip('\n')).strip()
            store.add_node(c, {'id': meta['id'], 'title': title, 'body': text, 'quotes': meta.get('quotes') or [], 'refs': meta.get('refs') or [],
                               'terms': meta.get('terms') or [], 'created': meta.get('created'), 'last_used': meta.get('last_used'),
                               'trust': meta.get('trust'), 'session': meta.get('session'), 'status': meta.get('status'),
                               'n_used': meta.get('n_used'), 'superseded_by': meta.get('superseded_by')},
                           origin=meta.get('source') or 'chat'); n_node += 1
            edges += [(meta['id'], k, w) for k, w in (meta.get('links') or {}).items()]
        for s, dd, w in edges: c.execute('INSERT OR REPLACE INTO edge(src, dst, weight) VALUES (?,?,?)', (s, dd, w))
        c.commit()
    finally: c.close()
    return {'sources': n_src, 'nodes': n_node}
