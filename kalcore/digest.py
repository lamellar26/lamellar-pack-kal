"""Nightly digest: a local model reads one window of chunks at a time and writes short nodes; a program checks every node.
Lessons kept from KAL (project 118): progress is saved per window (resume after a stop), a deadline ends the run cleanly,
yielding to a user job leaves the window pending (never counted as learnt), QC is done by code, not by another model."""
import json, re, time, unicodedata

PROMPT = """You are a note-taker. Read the numbered passages below and write notes that a person who can NOT see the passages will search later.

Rules (a program checks every note; a note that breaks a rule is thrown away):
1. Write only what the passages say. No outside knowledge, no interpretation the text does not state.
2. "quotes": 1-3 sentences copied EXACTLY, character for character, from the passages (copy, do not paraphrase, do not fix punctuation).
3. "refs": the labels (inside [ ]) of the passages the note comes from. Only labels shown below.
4. "terms": 2-6 key words or names that appear literally in those passages.
   Write in exactly the same characters as the passages: if they are in Traditional Chinese, write Traditional Chinese (never Simplified).
5. "title": short, specific (who / what / where). "body": 1-3 sentences, at most 150 characters, in the same language as the passages. Any number, name or verse reference in the body must appear in the passages. If the lines carry labels such as verse or section numbers (e.g. "8:28"), start the title with the label(s) of the lines the note is about.
6. Write {n_min}-{n_max} notes, each about ONE specific fact, claim or event, so a question about that fact finds it.

Reply with JSON only, no other text:
{{"nodes": [{{"title": "...", "body": "...", "quotes": ["..."], "refs": ["..."], "terms": ["..."]}}]}}

Passages:
{passages}"""

FIX = """Some notes failed the program check. Fix ONLY these notes and reply with the same JSON format (only the fixed notes):
{problems}"""


def norm(s):
    """For verbatim checks: NFKC, drop all whitespace and quote marks, unify common punctuation width."""
    s = unicodedata.normalize('NFKC', s or '')
    return re.sub(r'[\s"\'「」『』“”‘’]+', '', s)


def parse_json(text):
    """First JSON object in the model output (it sometimes wraps it in ``` or adds words)."""
    text = re.sub(r'<think>.*?</think>', '', text or '', flags=re.S)
    start = text.find('{')
    while start >= 0:
        depth = 0; ins = False; esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if ins:
                if esc: esc = False
                elif ch == '\\': esc = True
                elif ch == '"': ins = False
                continue
            if ch == '"': ins = True
            elif ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try: return json.loads(text[start:i + 1])
                    except ValueError: break
        start = text.find('{', start + 1)
    return None


def _cjk(s):
    """Share of Chinese in a text, counting each Chinese character and each latin word as one token
    (so mixed business text such as "Bright Harbour 訂 40 部" counts as Chinese, a fully English note does not)."""
    han = len(re.findall(r'[\u4e00-\u9fff]', s or '')); words = len(re.findall(r'[A-Za-z]+', s or ''))
    return han / (han + words) if han + words else 0.0


NUM = re.compile(r'\d+(?:[:：.]\d+)*')


def qc(node, texts):
    """Program check of one node against the window texts {ref: text}. Returns None (pass) or the reason it failed."""
    if not isinstance(node, dict): return 'not an object'
    for k in ('title', 'body'):
        if not isinstance(node.get(k), str) or not node[k].strip(): return f'missing {k}'
    if isinstance(node.get('refs'), list):     # models often copy the label with its brackets: [a.md#1]
        node['refs'] = [r.strip().strip('[]').strip() if isinstance(r, str) else r for r in node['refs']]
    refs = node.get('refs') or []; quotes = node.get('quotes') or []; terms = node.get('terms') or []
    if not isinstance(refs, list) or not refs: return 'no refs (a note without refs is void)'
    bad = [r for r in refs if r not in texts]
    if bad: return f'refs not in the given passages: {bad}'
    if not isinstance(quotes, list) or not quotes: return 'no quotes'
    src = norm(''.join(texts[r] for r in refs))
    for q in quotes:
        nq = norm(q).rstrip('，,。.；;：:、！!？?')
        if len(nq) < 4: return f'quote too short: {q!r}'
        if nq not in src: return f'quote not found word for word in its refs: {q!r}'
    if not isinstance(terms, list): return 'terms must be a list'
    for t in terms:
        if not isinstance(t, str) or norm(t) not in src: return f'term not in its refs: {t!r}'
    if len(node['body']) > 300: return 'body too long'
    if _cjk(''.join(quotes)) > 0.3 and (_cjk(node['body']) < 0.3 or _cjk(node['title'] + node['body']) < 0.3):   # judged by what it quotes (sources may mix languages)     # notes in another language dodge the name/term checks
        return 'title/body must be written in the same language as the passages'
    for n in NUM.findall(node['title'] + ' ' + node['body']):
        if n not in src: return f'number {n!r} is not in its refs'
    return None


def window_texts(c, wid):
    sid, a, b = c.execute('SELECT source_id, ord_from, ord_to FROM window WHERE id=?', (wid,)).fetchone()
    return {r: t for r, t in c.execute('SELECT ref, text FROM chunk WHERE source_id=? AND ord BETWEEN ? AND ? AND active=1 ORDER BY ord', (sid, a, b))}


def digest_window(c, wid, chat, retries=2, review=None):
    """chat(messages) -> text. Returns (ok_nodes, rejected_count, note). Writes nothing; the caller stores."""
    texts = window_texts(c, wid)
    if not texts: return [], 0, 'empty window'
    size = sum(len(t) for t in texts.values())
    n_min, n_max = max(2, size // 700), max(3, size // 300)
    passages = '\n\n'.join(f'[{r}]\n{t}' for r, t in texts.items())
    msgs = [{'role': 'user', 'content': PROMPT.format(passages=passages, n_min=n_min, n_max=n_max)}]
    good, rejected, pending = [], 0, None
    for attempt in range(retries + 1):
        out = chat(msgs)
        data = parse_json(out)
        nodes = data.get('nodes') if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            if attempt == retries: return good, rejected + len(pending or []), 'model output was not valid JSON'
            msgs += [{'role': 'assistant', 'content': out}, {'role': 'user', 'content': 'That was not valid JSON. Reply with the JSON object only.'}]
            continue
        problems = []
        for n in nodes:
            why = qc(n, texts)
            if why is None and review:
                why = review(n, texts)
            if why is None: good.append({k: n[k] for k in ('title', 'body', 'quotes', 'refs', 'terms') if k in n})
            else: problems.append((n, why))
        pending = problems
        if not problems: break
        if attempt == retries: rejected += len(problems); break
        msgs += [{'role': 'assistant', 'content': out},
                 {'role': 'user', 'content': FIX.format(problems='\n'.join(f'- {json.dumps(n, ensure_ascii=False)[:400]}\n  problem: {w}' for n, w in problems))}]
    return good, rejected, ''


def learn(c, chat, deadline=None, busy=None, max_tries=3, review=None, log=print):
    """Digest pending windows until done, deadline (epoch secs) or busy() says a user job is running.
    Returns dict(done, failed, nodes, rejected, stopped)."""
    from .store import add_node
    st = dict(done=0, failed=0, nodes=0, rejected=0, stopped='')
    rows = c.execute("""SELECT w.id FROM window w JOIN source s ON s.id=w.source_id AND s.active=1 AND s.sha=w.sha
                        WHERE w.status='pending' OR (w.status='failed' AND w.tries<?) ORDER BY w.id""", (max_tries,)).fetchall()
    for (wid,) in rows:
        if deadline and time.time() > deadline: st['stopped'] = 'deadline'; break
        if busy and busy(): st['stopped'] = 'user job running'; break      # window stays pending: not counted as learnt
        t0 = time.time()
        try: good, rej, note = digest_window(c, wid, chat, review=review)
        except Exception as e: good, rej, note = [], 0, f'{type(e).__name__}: {e}'
        if busy and busy() and not good: st['stopped'] = 'user job running'; break
        failed = bool(note) and not good
        for n in good: add_node(c, n, wid)
        c.execute('UPDATE window SET status=?, tries=tries+1, nodes_ok=nodes_ok+?, nodes_bad=nodes_bad+?, note=?, updated=? WHERE id=?',
                  ('failed' if failed else 'done', len(good), rej, note, int(time.time()), wid))
        c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('last_learn', ?)", (time.strftime('%Y-%m-%d %H:%M'),))
        c.commit()
        st['failed' if failed else 'done'] += 1; st['nodes'] += len(good); st['rejected'] += rej
        log(f'window {wid}: {len(good)} ok, {rej} rejected{", " + note if note else ""} ({time.time() - t0:.0f}s)')
    return st
