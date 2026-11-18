"""KAL library: folder -> passages -> SQLite (fts5 trigram); search over the passages.
Standard library only. Nothing is ever deleted: changed files mark old passages inactive.
(Digest notes were removed 11-17: in KL4 and KL9 they added nothing over searching the original text.)"""
import hashlib, math, os, re, sqlite3, time, zipfile
from xml.etree import ElementTree as ET
from .words import W

EXTS = ('.md', '.txt', '.docx')
TARGET, HARD = 500, 1000          # chunk size (characters)
STOP = set(W['stop_chars'])
SHORT_W = 0.6                     # weight of a 2-character Chinese n-gram relative to longer terms
SCHEMA = """
CREATE TABLE IF NOT EXISTS source (id INTEGER PRIMARY KEY, root TEXT, path TEXT, rel TEXT, sha TEXT, active INTEGER DEFAULT 1, added INTEGER);
CREATE TABLE IF NOT EXISTS chunk (id INTEGER PRIMARY KEY, source_id INTEGER, ord INTEGER, ref TEXT, text TEXT, active INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS chunk_ref ON chunk(ref, active);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(text, tokenize='trigram');
"""


def lib_path(cfg, lib='default'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', lib): raise ValueError(f'bad library name: {lib!r}')
    d = os.path.join(os.path.expanduser(cfg.get('state_dir') or '~/.lamellar'), 'kal'); os.makedirs(d, exist_ok=True)
    return os.path.join(d, lib + '.db')


def connect(path):
    """Old libraries may still hold node/edge/window tables from the digest era; they are left alone and unused."""
    c = sqlite3.connect(path, timeout=30); c.executescript(SCHEMA)
    return c


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
    for piece in re.split(r'(?<=[\u3002\uff01\uff1f\uff1b.!?;])', line):
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
            st['changed'] += 1
        else: st['added'] += 1
        sid = c.execute('INSERT INTO source(root, path, rel, sha, added) VALUES (?,?,?,?,?)', (folder, fp, rel, sha, now)).lastrowid
        parts = split(text)
        for i, t in enumerate(parts):
            cid = c.execute('INSERT INTO chunk(source_id, ord, ref, text) VALUES (?,?,?,?)', (sid, i + 1, f'{rel}#{i + 1}', t)).lastrowid
            c.execute('INSERT INTO chunk_fts(rowid, text) VALUES (?,?)', (cid, t))
        st['chunks'] += len(parts)
    c.commit()
    return st


# ---------- search ----------
def query_terms(q, max_terms=40):
    """Sentence -> terms: raw words (split on punctuation/space) + Chinese 3-/2-grams (stop characters removed) + latin words."""
    raw = [t for t in re.split(r'[\s,\uff0c\u3001\u3002\uff1f\uff01\uff1a\uff1b\u300c\u300d\u300e\u300f\uff08\uff09()\u300a\u300b\u3008\u3009\[\]"?!.;:]+', q or '') if t]
    out, seen = [], set()
    def add(t):
        t = t.lower()
        if t in seen or len(t) < 2: return
        if re.fullmatch(r'[a-z][a-z\-]*', t) and (len(t) < 3 or t in EN_STOP): return     # short / function English words
        seen.add(t); out.append(t)
    for t in raw:
        if re.search(r'[\u4e00-\u9fff]', t) and len(t) <= 6 and not any(ch in STOP for ch in t): add(t)
        # n-grams inside each run of Chinese characters between stop characters (joining across a removed
        # stop character made fake grams that are not in the text)
        for han in re.findall(r'[\u4e00-\u9fff]+', t):
            for run in re.split('[' + ''.join(STOP) + ']+', han):
                for n in (3, 2):
                    for i in range(len(run) - n + 1): add(run[i:i + n])
        for m in re.findall(r'[A-Za-z0-9][A-Za-z0-9\-.:]{1,}', t): add(m)
    return out[:max_terms]


EN_STOP = set('the and for are was were with that this from what which who how why when where does did has have had not but you your '
              'can will would should could into about than then them they their there here its our out all any per via'.split())


def _weight(t): return SHORT_W if len(t) == 2 and re.fullmatch(r'[\u4e00-\u9fff]{2}', t) else 1.0


def _match_ids(c, term):
    """rowids of live passages that contain term. Terms of 3+ characters use the trigram index; 2-character terms use LIKE."""
    if len(term) >= 3:
        q = '"' + term.replace('"', '""') + '"'
        return {r[0] for r in c.execute('SELECT chunk_fts.rowid FROM chunk_fts JOIN chunk ON chunk.rowid=chunk_fts.rowid WHERE chunk_fts MATCH ? AND chunk.active=1', (q,))}
    return {r[0] for r in c.execute('SELECT rowid FROM chunk WHERE text LIKE ? AND active=1', ('%' + term + '%',))}


def _rank(c, terms, k):
    n = c.execute('SELECT count(*) FROM chunk WHERE active=1').fetchone()[0]
    if not n or not terms: return []
    total, score = 0.0, {}
    for t in terms:
        ids = _match_ids(c, t)
        w = _weight(t) * math.log(1 + n / (len(ids) or 1))
        if not ids: w = _weight(t) * math.log(1 + n)        # a term the library has never seen still counts in the denominator
        total += w
        for i in ids: score[i] = score.get(i, 0.0) + w
    top = sorted(score.items(), key=lambda x: -x[1])[:k]
    return [(rid, round(s / total, 3)) for rid, s in top]


def search(c, q, k=5, min_rel=0.0, rel_top=0.0):
    """Passages [{'ref', 'text', 'rel'}], rel 0..1 = share of the query's weighted terms found.
    min_rel: absolute floor. rel_top: also drop results below rel_top x the best result (keeps only the clear winners)."""
    ranked = _rank(c, query_terms(q), k); out = []
    for rid, rel in ranked:
        if rel < min_rel or rel < rel_top * ranked[0][1]: continue
        ref, text = c.execute('SELECT ref, text FROM chunk WHERE rowid=?', (rid,)).fetchone()
        out.append({'ref': ref, 'text': text, 'rel': rel})
    return out


def read_ref(c, ref):
    r = c.execute('SELECT c.text, c.ord, c.source_id, s.rel FROM chunk c JOIN source s ON s.id=c.source_id WHERE c.ref=? AND c.active=1', (ref,)).fetchone()
    if r:
        text, ord_, sid, rel = r
        nb = [x[0] for x in c.execute('SELECT ref FROM chunk WHERE source_id=? AND ord IN (?,?) AND active=1 ORDER BY ord', (sid, ord_ - 1, ord_ + 1))]
        return {'ref': ref, 'text': text, 'neighbours': nb}
    return None


def status(c):
    g = lambda sql: c.execute(sql).fetchone()[0]
    return {'files': g('SELECT count(*) FROM source WHERE active=1'), 'chunks': g('SELECT count(*) FROM chunk WHERE active=1')}
