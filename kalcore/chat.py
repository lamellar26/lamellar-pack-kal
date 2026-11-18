"""Conversation -> the user's private KAL library (KL6).

After the answer is delivered (the user does not wait), what the USER said and what TOOLS returned in that exchange is
saved as a library source, searchable at once:
  - only when there is something factual: a tool was used, or the user stated facts (pure chat is not saved)
  - the assistant's own words are never saved (an answer can not later prove itself)
  - never-remember content (passwords, phone numbers, emails, money amounts, memory_deny) is masked before it is stored
  - one library per user (chat-<bot>-<chat>.db); users can not see each other's
No model is called. Markdown is the source of truth: <state>/kal/<lib>/sources/*.md; `lamellar kal rebuild --lib <lib>` rebuilds the db.
(Model-written notes, links and fading were removed 11-17: in the KL9 replay they invented details in 3 of 24 notes.)
"""
import hashlib, json, os, re, time
from . import store

SRC_CAP, SRC_TOTAL = 4000, 12000
MARK = '<<<kal-ref {}>>>'


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


NOTE_CUE = re.compile(r'記底|記低|記住|記下|记住|记下|记底|幫我記|帮我记|備忘|备忘|'
                      r'\b(note|jot) (this|that|it|down)\b|\b(take|make) a note\b|\bremember (this|that)\b', re.I)
NOTED = 'kal_note saved'      # prefix of a successful kal_note result (so the after-delivery step does not save it twice)


def wants_note(text):
    """The user explicitly asked to keep something ("記底", "note this"): saved whatever the length filter says."""
    t = (text or '').strip()
    return bool(NOTE_CUE.search(t)) and not QUESTION_END.search(t)


def should_write(task, tools_used, tool_texts):
    """KL6 gate: pure chat never calls the model. An explicit "記底" is always saved."""
    if wants_note(task): return True
    if GREETING.match(task or ''): return False
    if tools_used and any(t.strip() for t in tool_texts): return True
    return bool(stated_facts(task))


def this_turn(task, messages):
    """The tool results of this turn only (messages also hold earlier turns of the session)."""
    start = 0
    for i, m in enumerate(messages):
        if m.get('role') == 'user' and isinstance(m.get('content'), str) and task and task[:200] in m['content']: start = i
    return [m.get('content') or '' for m in messages[start:] if m.get('role') == 'tool' and not (m.get('content') or '').startswith(NOTED)]


def previous_user(task, messages):
    """The user's message before this one (for a bare "記底" that points at what was just said)."""
    users = [m['content'] for m in messages if m.get('role') == 'user' and isinstance(m.get('content'), str)]
    for i in range(len(users) - 1, -1, -1):
        if task and task[:200] in users[i]: return users[i - 1].split('\n\n[kal]')[0] if i > 0 else ''
    return ''


def save_note(cfg, session, user_text, note, now=None):
    """kal_note: the user asked to keep something. Stored as the user's message + the note, searchable at once.
    Returns (ref, stored text) — the stored text is what is really in the library (never-remember content masked)."""
    lib = lib_name(cfg); c = store.connect(store.lib_path(cfg, lib))
    try:
        texts = save_source(c, cfg, lib, session, user_text, [note], now=now, kind='note'); c.commit()
    finally: c.close()
    refs = [r for r in texts if '#t' in r]
    return (refs[0] if refs else ''), '\n'.join(texts[r] for r in refs)


def _redact(text, deny):
    for pat, what in deny:
        text = re.sub(pat, f'[{what} removed]', text)
    return text


def deny_list(cfg):
    try:
        from lamellar.memory import DENY
    except Exception: DENY = []
    return list(DENY) + [(p, 'configured never-remember content') for p in cfg.get('memory_deny') or []]


def save_source(c, cfg, lib, session, task, tool_texts, now=None, kind='chat'):
    """Store what the user said and what tools returned as a library source (searchable at once) + a markdown file.
    Never-remember content is masked before it is stored. Returns {ref: text}."""
    now = int(now or time.time()); deny = deny_list(cfg)
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    rel = f"chat/{kind + '-' if kind != 'chat' else ''}{stamp}-{hashlib.sha1((session + task + str(time.time_ns())).encode()).hexdigest()[:6]}"
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


def learn_exchange(cfg, task, messages, tools_used=0, session='', now=None, noted=False):
    """The whole after-delivery step. Returns a summary dict (never raises into the caller's job; the hook wraps it)."""
    tool_texts = this_turn(task, messages)
    if noted and not any(t.strip() for t in tool_texts): return {'skipped': 'already saved by kal_note'}
    if not should_write(task, tools_used, tool_texts): return {'skipped': 'nothing factual (pure chat)'}
    if wants_note(task) and not noted:          # "記底" but the model did not call kal_note: keep it anyway
        bare = NOTE_CUE.sub('', task).strip(' \t\n,，。.!！:：~')
        prev = previous_user(task, messages) if len(bare) < 8 else ''
        if prev: tool_texts = [f'(the message the user asked to keep)\n{prev}'] + tool_texts
    lib = lib_name(cfg); c = store.connect(store.lib_path(cfg, lib))
    try:
        texts = save_source(c, cfg, lib, session, task, tool_texts, now=now); c.commit()
        return {'lib': lib, 'source_refs': len(texts)} if texts else {'skipped': 'empty source'}
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
    d = os.path.join(md_dir(cfg, lib), 'sources'); p = store.lib_path(cfg, lib)
    if os.path.isfile(p): os.replace(p, p + time.strftime('.bak-%Y%m%d-%H%M%S'))
    c = store.connect(p); n = 0
    try:
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, fn), encoding='utf-8') as f: meta, body = _fm(f.read())
            parts = re.split(r'^<<<kal-ref (.+?)>>>\n', body, flags=re.M)
            texts = {parts[i]: parts[i + 1].rstrip('\n') for i in range(1, len(parts) - 1, 2)}
            _insert_source(c, str(meta.get('rel')), str(meta.get('session') or ''), int(meta.get('time') or 0), texts); n += 1
        c.commit()
    finally: c.close()
    return {'sources': n}
