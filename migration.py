"""Importa un backup Nesso senza eseguirne il codice o modificare lo ZIP."""
import hashlib
import io
import json
import zipfile

LIMIT = 200_000_000


def inspect_backup(raw):
    if len(raw) > LIMIT:
        raise ValueError('Backup troppo grande (massimo 200 MB).')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        entries = z.infolist()
        names = [e.filename for e in entries]
        if len(names) != len(set(names)) or len(names) > 2000:
            raise ValueError('Archivio con nomi duplicati o troppi file.')
        if sum(e.file_size for e in entries) > LIMIT:
            raise ValueError('Backup estratto troppo grande.')
        manifest = json.loads(z.read('manifest.json'))
        if manifest.get('formato') != 'ius-studio-backup':
            raise ValueError('Seleziona un backup ZIP di Nesso.')
        files = {e.filename: z.read(e) for e in entries if not e.is_dir()}
        for entry in manifest['file']:
            body = files[entry['percorso']]
            if len(body) != entry['byte'] or hashlib.sha256(body).hexdigest() != entry['sha256']:
                raise ValueError('Integrita del backup non valida.')
        data = json.loads(files['dati/studio.json'])
        books = data['quaderni']
        if not isinstance(books, dict):
            raise ValueError('Quaderni non validi.')
        for name, pages in books.items():
            if not isinstance(name, str) or not isinstance(pages, list):
                raise ValueError('Quaderno non valido.')
            for page in pages:
                if not isinstance(page, dict) or not all(isinstance(page.get(k), str) for k in ('titolo', 'testo')):
                    raise ValueError('Pagina non valida.')
        maps = json.loads(files.get('dati/mappe.json', b'{"mappe":[]}'))
        history = json.loads(files.get('dati/cronologia.json', b'{"appunti":{}}'))
        return {
            'raw': raw, 'digest': hashlib.sha256(raw).hexdigest(), 'data': data,
            'maps': maps, 'history': history,
            'summary': '{} quaderni, {} pagine, {} mappe, {} versioni'.format(
                len(books), sum(len(p) for p in books.values()), len(maps.get('mappe', [])),
                sum(len(v) for v in history.get('appunti', {}).values()))}


def ensure_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS imports (
            digest TEXT PRIMARY KEY, original_zip BLOB NOT NULL,
            summary TEXT NOT NULL, first_notebook INTEGER);
        CREATE TABLE IF NOT EXISTS legacy_pages (
            page_id INTEGER PRIMARY KEY REFERENCES pages(id),
            digest TEXT NOT NULL REFERENCES imports(digest),
            notebook_name TEXT NOT NULL, original_index INTEGER NOT NULL,
            original_json TEXT NOT NULL);
    ''')


def import_backup(db, bundle):
    digest = bundle['digest']
    prior = db.execute('SELECT first_notebook FROM imports WHERE digest=?', (digest,)).fetchone()
    if prior:
        return prior[0], False
    first = None
    with db:
        db.execute('INSERT INTO imports(digest,original_zip,summary) VALUES (?,?,?)',
                   (digest, bundle['raw'], bundle['summary']))
        for name, pages in bundle['data']['quaderni'].items():
            book = db.execute('INSERT INTO notebooks(name) VALUES (?)', (name,)).lastrowid
            if first is None:
                first = book
            for index, note in enumerate(pages):
                page_id = db.execute('INSERT INTO pages(notebook_id,title,body) VALUES (?,?,?)',
                                     (book, note['titolo'], note['testo'])).lastrowid
                db.execute('INSERT INTO legacy_pages VALUES (?,?,?,?,?)',
                           (page_id, digest, name, index, json.dumps(note, ensure_ascii=False)))
        db.execute('UPDATE imports SET first_notebook=? WHERE digest=?', (first, digest))
    return first, True


def archive_text(db):
    output = []
    for raw, summary in db.execute('SELECT original_zip,summary FROM imports'):
        bundle = inspect_backup(raw)
        output += ['BACKUP IMPORTATO', summary, '\nMAPPE']
        for index, m in enumerate(bundle['maps'].get('mappe', []), 1):
            output.append('\nMappa {}'.format(index))
            for node in m.get('nodi', []):
                output.append('{}: {}\n{}'.format(node.get('titolo', ''), node.get('relazione', ''), node.get('testo', '')))
        output.append('\nCRONOLOGIA')
        for ref, versions in bundle['history'].get('appunti', {}).items():
            book, index = json.loads(ref)
            output.append('\n{} / pagina {}'.format(book, index + 1))
            for v in versions:
                note = v.get('nota', {})
                output.append('\n{} — {}\n{}'.format(v.get('salvata_il', ''), note.get('titolo', ''), note.get('testo', '')))
        output.append('\nMETADATI E FOGLI\nIl backup originale completo e conservato nel database. '
                      'La modifica visuale delle mappe e dei disegni non e ancora disponibile.')
    return '\n'.join(output) or 'Nessun backup importato.'
