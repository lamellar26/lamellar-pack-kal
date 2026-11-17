"""KAL library: folder -> chunks -> SQLite (fts5 trigram) ; nodes written by the nightly digest; search over both.
Standard library only. Nothing is ever deleted: changed files mark old chunks inactive, old nodes superseded."""
import hashlib, json, math, os, re, sqlite3, time, zipfile
from xml.etree import ElementTree as ET

EXTS = ('.md', '.txt', '.docx')
TARGET, HARD = 500, 1000          # chunk size (characters)
WINDOW = 2500                     # digest window size (characters)
STOP = set('的了是在有和與及或但而就都也很到為以被把對從向於之其此這那咩係嘅喺同唔咗啲點乜邊個嗎呢吧啊呀嘛甚麼什麼哪嗰佢哋嘢冇噉咁啦喎囉嚟')
SHORT_W = 0.6                     # weight of a 2-character Chinese n-gram relative to longer terms
SCHEMA = """
CREATE TABLE IF NOT EXISTS source (id INTEGER PRIMARY KEY, root TEXT, path TEXT, rel TEXT, sha TEXT, active INTEGER DEFAULT 1, added INTEGER);
CREATE TABLE IF NOT EXISTS chunk (id INTEGER PRIMARY KEY, source_id INTEGER, ord INTEGER, ref TEXT, text TEXT, active INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS chunk_ref ON chunk(ref, active);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS node (id TEXT PRIMARY KEY, title TEXT, body TEXT, quotes TEXT, refs TEXT, terms TEXT,
  origin TEXT DEFAULT 'digest', status TEXT DEFAULT 'ok', n_used INTEGER DEFAULT 1, last_used INTEGER, created INTEGER, window_id INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS edge (src TEXT, dst TEXT, weight REAL DEFAULT 1.0, PRIMARY KEY (src, dst));
CREATE TABLE IF NOT EXISTS window (id INTEGER PRIMARY KEY, source_id INTEGER, sha TEXT, ord_from INTEGER, ord_to INTEGER,
  status TEXT DEFAULT 'pending', tries INTEGER DEFAULT 0, nodes_ok INTEGER DEFAULT 0, nodes_bad INTEGER DEFAULT 0, note TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def lib_path(cfg, lib='default'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', lib): raise ValueError(f'bad library name: {lib!r}')
    d = os.path.join(os.path.expanduser(cfg.get('state_dir') or '~/.lamellar'), 'kal'); os.makedirs(d, exist_ok=True)
    return os.path.join(d, lib + '.db')


NODE_COLS = {'trust': "TEXT DEFAULT 'normal'", 'session': 'TEXT', 'superseded_by': 'TEXT'}   # added for conversation notes (KL7/KL8)
TRUST_W = {'low': 0.7, 'normal': 1.0}


def connect(path):
    c = sqlite3.connect(path, timeout=30); c.executescript(SCHEMA)
    have = {r[1] for r in c.execute('PRAGMA table_info(node)')}
    for col, typ in NODE_COLS.items():
        if col not in have: c.execute(f'ALTER TABLE node ADD COLUMN {col} {typ}')
    return c


def activation(n_used, last_used, trust, now=None, origin='chat'):
    """ACT-R style strength of a conversation note (0..1): use count against time since last use (decay d=0.5),
    times trust. Notes digested from documents do not fade (1.0)."""
    if origin != 'chat': return 1.0
    age_days = max(0.0, ((now or time.time()) - (last_used or now or time.time())) / 86400)
    return TRUST_W.get(trust or 'normal', 1.0) * min(1.0, math.sqrt(max(1, n_used or 1)) / math.sqrt(1 + age_days))


# ---------- reading files ----------
def read_docx(path):
    ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    with zipfile.ZipFile(path) as z: root = ET.fromstring(z.read('word/document.xml'))
    paras = []
    for p in root.iter(ns + 'p'):
        t = ''.join(x.text or '' for x in p.iter(ns + 't'))
        paras.append(t)
    return '\n\n'.join(x for x in paras if x.strip())


def read_text(path):
    if path.lower().endswith('.docx'): return read_docx(path)
    with open(path, 'rb') as f: raw = f.read()
    for enc in ('utf-8', 'utf-16', 'big5', 'gb18030'):
        try: return raw.decode(enc)
        except UnicodeDecodeError: continue
    return raw.decode('utf-8', 'replace')


def _split_long(line):
    parts, cur = [], ''
    for piece in re.split(r'(?<=[。！？；.!?;])', line):
        if len(cur) + len(piece) > HARD and cur: parts.append(cur); cur = ''
        while len(piece) > HARD: parts.append(piece[:HARD]); piece = piece[HARD:]
        cur += piece
    if cur: parts.append(cur)
    return parts


def split(text):
    """Paragraph-aware chunks of about TARGET characters, never above HARD."""
    chunks, cur = [], ''
    for para in re.split(r'\n\s*\n', text.replace('\r\n', '\n')):
        para = para.strip()
        if not para: continue
        for line in [l for l in para.split('\n') if l.strip()]:
            for piece in _split_long(line.strip()):
                if cur and len(cur) + len(piece) + 1 > HARD: chunks.append(cur); cur = ''
                cur = (cur + '\n' + piece) if cur else piece
        if len(cur) >= TARGET: chunks.append(cur); cur = ''
        elif cur: cur += '\n'
    if cur.strip(): chunks.append(cur.strip())
    return [c.strip() for c in chunks if c.strip()]


def _files(folder):
    for d, subs, names in os.walk(folder):
        subs[:] = sorted(s for s in subs if not s.startswith('.'))
        for n in sorted(names):
            if n.lower().endswith(EXTS) and not n.startswith(('.', '~$')): yield os.path.join(d, n)


def add_folder(c, folder):
    """Index a folder (or one file). Unchanged files are skipped; changed files replace their chunks (old ones kept, inactive).
    Returns dict(added, changed, skipped, chunks)."""
    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.exists(folder): raise FileNotFoundError(folder)
    root_name = os.path.basename(folder.rstrip(os.sep)) or 'root'
    files = [folder] if os.path.isfile(folder) else list(_files(folder))
    st = dict(added=0, changed=0, skipped=0, chunks=0, unreadable=0)
    now = int(time.time())
    for fp in files:
        rel = root_name if os.path.isfile(folder) else root_name + '/' + os.path.relpath(fp, folder).replace(os.sep, '/')
        try: text = read_text(fp)
        except Exception: st['unreadable'] += 1; continue
        sha = hashlib.sha1(text.encode('utf-8')).hexdigest()
        old = c.execute('SELECT id, sha FROM source WHERE path=? AND active=1', (fp,)).fetchone()
        if old and old[1] == sha: st['skipped'] += 1; continue
        if old:
            c.execute('UPDATE source SET active=0 WHERE id=?', (old[0],))
            c.execute('UPDATE chunk SET active=0 WHERE source_id=?', (old[0],))
            olds = {r[0] for r in c.execute('SELECT ref FROM chunk WHERE source_id=?', (old[0],))}
            for nid, refs in c.execute("SELECT id, refs FROM node WHERE status='ok'").fetchall():
                if set(json.loads(refs or '[]')) & olds: c.execute("UPDATE node SET status='superseded' WHERE id=?", (nid,))
            st['changed'] += 1
        else: st['added'] += 1
        sid = c.execute('INSERT INTO source(root, path, rel, sha, added) VALUES (?,?,?,?,?)', (folder, fp, rel, sha, now)).lastrowid
        parts = split(text); win, wlen, wstart = [], 0, 0
        for i, t in enumerate(parts):
            ref = f'{rel}#{i + 1}'
            cid = c.execute('INSERT INTO chunk(source_id, ord, ref, text) VALUES (?,?,?,?)', (sid, i + 1, ref, t)).lastrowid
            c.execute('INSERT INTO chunk_fts(rowid, text) VALUES (?,?)', (cid, t))
            if win and wlen + len(t) > WINDOW:
                c.execute('INSERT INTO window(source_id, sha, ord_from, ord_to, updated) VALUES (?,?,?,?,?)', (sid, sha, wstart, i, now))
                win, wlen = [], 0
            if not win: wstart = i + 1
            win.append(t); wlen += len(t)
        if win: c.execute('INSERT INTO window(source_id, sha, ord_from, ord_to, updated) VALUES (?,?,?,?,?)', (sid, sha, wstart, len(parts), now))
        st['chunks'] += len(parts)
    c.commit()
    return st


# ---------- search ----------
def query_terms(q, max_terms=40):
    """Sentence -> terms: raw words (split on punctuation/space) + Chinese 3-/2-grams (stop characters removed) + latin words."""
    raw = [t for t in re.split(r'[\s,，、。？！：；「」『』（）()《》〈〉\[\]"?!.;:]+', q or '') if t]
    out, seen = [], set()
    def add(t):
        t = t.lower()
        if t in seen or len(t) < 2: return
        if re.fullmatch(r'[a-z][a-z\-]*', t) and (len(t) < 3 or t in EN_STOP): return     # short / function English words
        seen.add(t); out.append(t)
    for t in raw:
        if re.search(r'[一-鿿]', t) and len(t) <= 6 and not any(ch in STOP for ch in t): add(t)
        # n-grams inside each run of Chinese characters between stop characters (joining across a removed
        # stop character made fake grams such as 羅寫羅 out of 保羅寫羅馬書)
        for han in re.findall(r'[一-鿿]+', t):
            for run in re.split('[' + ''.join(STOP) + ']+', han):
                for n in (3, 2):
                    for i in range(len(run) - n + 1): add(run[i:i + n])
        for m in re.findall(r'[A-Za-z0-9][A-Za-z0-9\-.:]{1,}', t): add(m)
    return out[:max_terms]


EN_STOP = set('the and for are was were with that this from what which who how why when where does did has have had not but you your '
              'can will would should could into about than then them they their there here its our out all any per via'.split())


def _weight(t): return SHORT_W if len(t) == 2 and re.fullmatch(r'[一-鿿]{2}', t) else 1.0


def _match_ids(c, fts, base, term):
    """rowids whose text contains term. Terms of 3+ characters use the trigram index; 2-character terms use LIKE."""
    if len(term) >= 3:
        q = '"' + term.replace('"', '""') + '"'
        return {r[0] for r in c.execute(f'SELECT {fts}.rowid FROM {fts} JOIN {base} ON {base}.rowid={fts}.rowid WHERE {fts} MATCH ? AND {_live(base)}', (q,))}
    return {r[0] for r in c.execute(f'SELECT rowid FROM {base} WHERE {_text(base)} LIKE ? AND {_live(base)}', ('%' + term + '%',))}


def _live(base): return 'chunk.active=1' if base == 'chunk' else "node.status='ok'"
def _text(base): return 'text' if base == 'chunk' else "(title || ' ' || body || ' ' || quotes || ' ' || terms)"


def _rank(c, fts, base, terms, k):
    n = c.execute(f'SELECT count(*) FROM {base} WHERE {_live(base)}').fetchone()[0]
    if not n or not terms: return []
    total, score = 0.0, {}
    for t in terms:
        ids = _match_ids(c, fts, base, t)
        w = _weight(t) * math.log(1 + n / (len(ids) or 1))
        if not ids: w = _weight(t) * math.log(1 + n)        # a term the library has never seen still counts in the denominator
        total += w
        for i in ids: score[i] = score.get(i, 0.0) + w
    top = sorted(score.items(), key=lambda x: -x[1])[:k]
    return [(rid, round(s / total, 3)) for rid, s in top]


def search(c, q, k=5, min_rel=0.0, nodes=True, chunks=True, rel_top=0.0):
    """Returns {'nodes': [dict], 'chunks': [dict]} each with rel (0..1: share of the query's weighted terms found).
    min_rel: absolute floor. rel_top: also drop results below rel_top x the best result (keeps only the clear winners)."""
    terms = query_terms(q); out = {'nodes': [], 'chunks': []}
    def keep(ranked): return [(i, r) for i, r in ranked if r >= min_rel and (not ranked or r >= rel_top * ranked[0][1])]
    if chunks:
        for rid, rel in keep(_rank(c, 'chunk_fts', 'chunk', terms, k)):
            ref, text = c.execute('SELECT ref, text FROM chunk WHERE rowid=?', (rid,)).fetchone()
            out['chunks'].append({'ref': ref, 'text': text, 'rel': rel})
    if nodes:
        now = time.time(); got = []
        for rid, rel in _rank(c, 'node_fts', 'node', terms, k * 3):
            r = c.execute('SELECT id, title, body, quotes, refs, origin, n_used, last_used, trust FROM node WHERE rowid=?', (rid,)).fetchone()
            a = activation(r[6], r[7], r[8], now, r[5])     # conversation notes fade unless used; documents do not
            got.append({'id': r[0], 'title': r[1], 'body': r[2], 'quotes': json.loads(r[3] or '[]'), 'refs': json.loads(r[4] or '[]'),
                        'origin': r[5], 'trust': r[8] or 'normal', 'rel': round(rel * a, 3)})
        got.sort(key=lambda x: -x['rel']); got = got[:k]
        out['nodes'] = [g for g in got if g['rel'] >= min_rel and g['rel'] >= rel_top * got[0]['rel']]
    return out


def read_ref(c, ref):
    r = c.execute('SELECT c.text, c.ord, c.source_id, s.rel FROM chunk c JOIN source s ON s.id=c.source_id WHERE c.ref=? AND c.active=1', (ref,)).fetchone()
    if r:
        text, ord_, sid, rel = r
        nb = [x[0] for x in c.execute('SELECT ref FROM chunk WHERE source_id=? AND ord IN (?,?) AND active=1 ORDER BY ord', (sid, ord_ - 1, ord_ + 1))]
        return {'ref': ref, 'text': text, 'neighbours': nb}
    r = c.execute('SELECT id, title, body, quotes, refs FROM node WHERE id=?', (ref,)).fetchone()
    if r: return {'ref': r[0], 'text': f'{r[1]}\n{r[2]}\nquotes: {r[3]}', 'neighbours': json.loads(r[4] or '[]')}
    return None


def add_node(c, node, window_id=None, origin='digest', trust='normal', session=None, now=None):
    nid = node.get('id') or 'n' + hashlib.sha1((node['title'] + json.dumps(node['refs'], ensure_ascii=False)).encode()).hexdigest()[:12]
    if c.execute('SELECT 1 FROM node WHERE id=?', (nid,)).fetchone(): return nid
    now = int(now or time.time())
    rid = c.execute('INSERT INTO node(id, title, body, quotes, refs, terms, origin, created, last_used, window_id, trust, session, status, n_used, superseded_by) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (nid, node['title'], node['body'], json.dumps(node.get('quotes', []), ensure_ascii=False),
                     json.dumps(node['refs'], ensure_ascii=False), json.dumps(node.get('terms', []), ensure_ascii=False), origin,
                     int(node.get('created') or now), int(node.get('last_used') or now), window_id, node.get('trust') or trust,
                     node.get('session') or session, node.get('status') or 'ok', int(node.get('n_used') or 1), node.get('superseded_by'))).lastrowid
    c.execute('INSERT INTO node_fts(rowid, text) VALUES (?,?)',
              (rid, ' '.join([node['title'], node['body'], ' '.join(node.get('quotes', [])), ' '.join(node.get('terms', []))])))
    return nid


def status(c):
    g = lambda sql: c.execute(sql).fetchone()[0]
    w = dict(c.execute("SELECT w.status, count(*) FROM window w JOIN source s ON s.id=w.source_id AND s.active=1 GROUP BY w.status").fetchall())
    ok, bad = c.execute('SELECT coalesce(sum(nodes_ok),0), coalesce(sum(nodes_bad),0) FROM window').fetchone()
    return {'files': g('SELECT count(*) FROM source WHERE active=1'), 'chunks': g('SELECT count(*) FROM chunk WHERE active=1'),
            'windows': sum(w.values()), 'windows_done': w.get('done', 0), 'windows_failed': w.get('failed', 0), 'windows_pending': w.get('pending', 0),
            'nodes': g("SELECT count(*) FROM node WHERE status='ok'"), 'qc_pass': ok, 'qc_reject': bad,
            'qc_rate': round(ok / (ok + bad), 3) if ok + bad else None,
            'last_learn': (c.execute("SELECT v FROM meta WHERE k='last_learn'").fetchone() or [None])[0]}
