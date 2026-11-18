"""
Campaign export engine.

Builds exports of a campaign's transcripts / recaps / per-player transcripts for
a chosen subset of sessions, in one of several formats:

    pdf  - one combined PDF with a cover page, table of contents (with page
           numbers) and a clickable bookmark outline
    md   - one combined Markdown document
    txt  - one combined plain-text document
    zip  - one folder per session, one file per item (the only format that can
           include per-player transcripts)

Exports run in a background thread and report progress through a small
in-memory registry (Scribble runs a single gunicorn worker, so process-local
state is sufficient). Finished files are written to a temp directory and
deleted after SCRIBBLE_EXPORT_TTL seconds (default 1 hour).

PDF generation is the slow path (xhtml2pdf is pure Python). To keep it
tolerable it renders each session as its own small document and merges them
with pypdf, which (a) gives real per-session progress, (b) allows cancelling,
(c) lets the work spread across CPU cores via SCRIBBLE_EXPORT_WORKERS, and
(d) avoids xhtml2pdf's poor scaling on one enormous document.
"""
import io
import os
import re
import time
import uuid
import zipfile
import logging
import tempfile
import threading
import multiprocessing
from datetime import datetime
from html import escape
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import markdown

EXPORT_DIR = os.path.join(tempfile.gettempdir(), 'scribble_exports')
TTL_SECONDS = int(os.environ.get('SCRIBBLE_EXPORT_TTL', '3600'))

FORMATS = {
    'pdf': 'pdf',
    'md': 'md',
    'txt': 'txt',
    'zip': 'zip',
}
CONTENT_TYPES = ('transcript', 'recap', 'players')

_jobs = {}
_lock = threading.Lock()
_run_slot = threading.Semaphore(1)   # one export at a time; the rest wait in "queued"


class ExportCancelled(Exception):
    pass


class RenderError(Exception):
    pass


class NothingToExport(Exception):
    pass


# ───────────────────────────── text helpers ─────────────────────────────

_RECAP_HEADER_MARKERS = ('🤖', '📋', '⌚', '🧾')
_SPEAKER_RE = re.compile(r'^((?:\[[^\]]*\]\s*[^:\n]{1,60}:)|(?:[^\[:\n][^:\n]{0,59}:))(.*)$')


def clean_recap(text):
    """Strip the generated stats header from a recap (everything before the first '##')."""
    header_ended = False
    out = []
    for line in (text or '').split('\n'):
        if '##' in line and not header_ended:
            header_ended = True
        if header_ended:
            out.append(line)
        elif not any(c in line for c in _RECAP_HEADER_MARKERS):
            out.append(line)
    return '\n'.join(out).strip()


def safe_name(value, fallback='export', limit=80):
    value = re.sub(r'[^\w.\- ]+', '_', value or '', flags=re.UNICODE).strip().replace(' ', '_')
    value = re.sub(r'_+', '_', value).strip('_.')
    return value[:limit] or fallback


def _date_labels(local_time_str, session_date):
    """Return (iso_date, pretty_date). Prefers the local time the UI shows."""
    try:
        d = datetime.strptime((local_time_str or '')[:10], '%Y-%m-%d')
        return d.strftime('%Y-%m-%d'), d.strftime('%B %d, %Y')
    except ValueError:
        pass
    if local_time_str and local_time_str.strip().lower() == 'unknown date':
        return 'unknown-date', 'Unknown date'
    if session_date:
        return session_date.strftime('%Y-%m-%d'), session_date.strftime('%B %d, %Y')
    return 'unknown-date', 'Unknown date'


def _session_title(meta):
    return f"Session {meta['number']}" if meta['number'] else "Undated session"


# ───────────────────────────── PDF building ─────────────────────────────

_PDF_CSS = """<style>
    @page { size: A4; margin: 2cm; }
    body { font-family: Helvetica, sans-serif; font-size: 10pt; line-height: 1.4; }
    h1 { color: #2c3e50; text-align: center; font-size: 24pt; margin-bottom: 20px; }
    h2 { color: #2980b9; border-bottom: 1px solid #eee; padding-bottom: 10px; margin-top: 0; }
    h3 { color: #2c3e50; margin-top: 4px; }
    .meta { color: #95a5a6; font-size: 9pt; font-style: italic; margin-bottom: 10px; }
    .speaker { font-weight: bold; color: #444; }
    .recap-content { text-align: justify; }
    .chunk { margin: 0 0 7px 0; line-height: 1.5; }
    .toc td { font-size: 11pt; padding: 2px 0; }
</style>"""

_CHUNK_LINES = 40   # lines per <p>; far cheaper for xhtml2pdf than one <div> per line


def _format_line(line):
    m = _SPEAKER_RE.match(line)
    if m:
        return f"<span class='speaker'>{escape(m.group(1), quote=False)}</span>{escape(m.group(2), quote=False)}"
    return escape(line, quote=False)


def _transcript_html(text):
    lines = [_format_line(l.strip()) for l in text.split('\n') if l.strip()]
    chunks = []
    for i in range(0, len(lines), _CHUNK_LINES):
        chunks.append("<p class='chunk'>" + "<br/>".join(lines[i:i + _CHUNK_LINES]) + "</p>")
    return "".join(chunks)


def _unit_html(meta, kind, text, show_label):
    body_label = {'recap': 'Recap', 'transcript': 'Transcript'}[kind]
    head = (f"<h2>{escape(_session_title(meta))}</h2>"
            f"<div class='meta'>{escape(meta['date_pretty'])}</div>")
    if show_label:
        head += f"<h3>{body_label}</h3>"
    if kind == 'recap':
        body = f"<div class='recap-content'>{markdown.markdown(clean_recap(text))}</div>"
    else:
        body = _transcript_html(text)
    return f"<html><head>{_PDF_CSS}</head><body>{head}{body}</body></html>"


def render_pdf(html_str):
    """Render one HTML document to PDF bytes. Top-level so worker processes can import it."""
    from xhtml2pdf import pisa
    buf = io.BytesIO()
    result = pisa.CreatePDF(html_str, dest=buf)
    if result.err:
        raise RenderError(f"xhtml2pdf reported {result.err} error(s)")
    return buf.getvalue()


def _page_count(pdf_bytes):
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def _cover_html(campaign_name, title_text):
    return (f"<html><head>{_PDF_CSS}</head><body>"
            f"<div style='text-align:center; margin-top:200px;'>"
            f"<h1>{escape(campaign_name)}</h1>"
            f"<h2 style='border:none;'>{escape(title_text)}</h2>"
            f"<p>Generated on {datetime.now().strftime('%Y-%m-%d')}</p></div></body></html>")


def _toc_html(entries):
    rows = "".join(
        f"<tr><td>{escape(label)}</td><td align='right'>{page}</td></tr>"
        for label, page in entries)
    return (f"<html><head>{_PDF_CSS}</head><body><h1>Table of Contents</h1>"
            f"<table class='toc' width='100%'>{rows}</table></body></html>")


def _worker_count():
    env = os.environ.get('SCRIBBLE_EXPORT_WORKERS')
    if env is not None:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return max(1, min(4, (os.cpu_count() or 1) - 1))


def _render_units(job, htmls):
    """Render a list of HTML docs to PDF bytes (in order), reporting progress."""
    results = [None] * len(htmls)
    workers = min(_worker_count(), len(htmls))

    if workers > 1:
        ex = None
        try:
            ex = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'))
            futures = {ex.submit(render_pdf, h): i for i, h in enumerate(htmls)}
            for fut in as_completed(futures):
                if job['cancel']:
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise ExportCancelled()
                results[futures[fut]] = fut.result()
                _bump(job)
            ex.shutdown()
            return results
        except (BrokenProcessPool, OSError) as e:
            logging.warning(f"Export worker pool unavailable ({e}); rendering in-process.")
            # Progress already counted for finished units is kept; the rest render below.
        finally:
            if ex:
                ex.shutdown(wait=False, cancel_futures=True)   # no-op if already shut down

    for i, h in enumerate(htmls):
        if results[i] is not None:
            continue
        if job['cancel']:
            raise ExportCancelled()
        results[i] = render_pdf(h)
        _bump(job)
    return results


def _build_pdf(job, sessions_iter, path):
    from pypdf import PdfReader, PdfWriter

    kinds = [k for k in ('recap', 'transcript') if k in job['content']]
    show_label = len(kinds) > 1
    title_text = {
        ('transcript',): 'Campaign Transcripts',
        ('recap',): 'Campaign Recaps',
    }.get(tuple(kinds), 'Campaign Transcripts & Recaps')

    _update(job, phase='Reading sessions…')
    htmls, labels = [], []   # labels: (meta, kind)
    for meta, data in sessions_iter:
        for kind in kinds:
            text = data.get(kind)
            if text:
                htmls.append(_unit_html(meta, kind, text, show_label))
                labels.append((meta, kind))
    if not htmls:
        raise NothingToExport("None of the selected sessions have the selected content.")

    _update(job, total=len(htmls), done=0, phase='Rendering PDF…')
    rendered = _render_units(job, htmls)

    _update(job, phase='Merging…')
    counts = [_page_count(b) for b in rendered]
    cover = render_pdf(_cover_html(job['campaign_name'], title_text))
    cover_pages = _page_count(cover)

    def toc_entries(first_unit_page):
        entries, page = [], first_unit_page
        for (meta, kind), n in zip(labels, counts):
            label = f"{_session_title(meta)}: {meta['date_pretty']}"
            if show_label:
                label += f" - {kind.capitalize()}"
            entries.append((label, page))
            page += n
        return entries

    toc = render_pdf(_toc_html(toc_entries(cover_pages + 2)))
    toc_pages = _page_count(toc)
    if toc_pages != 1:                      # TOC spilled onto extra pages; page numbers shift
        toc = render_pdf(_toc_html(toc_entries(cover_pages + toc_pages + 1)))

    writer = PdfWriter()
    for blob in (cover, toc):
        writer.append(PdfReader(io.BytesIO(blob)), import_outline=False)

    parents = {}
    for (meta, kind), blob, n in zip(labels, rendered, counts):
        start = len(writer.pages)
        writer.append(PdfReader(io.BytesIO(blob)), import_outline=False)
        title = f"{_session_title(meta)}: {meta['date_pretty']}"
        if show_label:
            parent = parents.get(meta['id'])
            if parent is None:
                parent = parents[meta['id']] = writer.add_outline_item(title, start)
            writer.add_outline_item(kind.capitalize(), start, parent=parent)
        else:
            writer.add_outline_item(title, start)

    writer.add_metadata({'/Title': f"{job['campaign_name']} - {title_text}"})
    with open(path, 'wb') as f:
        writer.write(f)


# ───────────────────── Markdown / text / zip builders ─────────────────────

def _combined_md(job, sessions_iter, path):
    kinds = [k for k in ('recap', 'transcript') if k in job['content']]
    show_label = len(kinds) > 1
    written = 0
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f"# {job['campaign_name']}\n\n"
                f"*Exported {datetime.now().strftime('%Y-%m-%d')}*\n")
        for meta, data in sessions_iter:
            sections = [(k, data.get(k)) for k in kinds if data.get(k)]
            if not sections:
                _bump(job)
                continue
            f.write(f"\n---\n\n## {_session_title(meta)} - {meta['date_pretty']}\n")
            for kind, text in sections:
                if show_label:
                    f.write(f"\n### {kind.capitalize()}\n")
                f.write("\n")
                if kind == 'recap':
                    f.write(clean_recap(text) + "\n")
                else:
                    # two trailing spaces = hard line break, so lines stay separate when rendered
                    f.write("  \n".join(l.rstrip() for l in text.split('\n') if l.strip()) + "\n")
            written += 1
            _bump(job)
    if not written:
        raise NothingToExport("None of the selected sessions have the selected content.")


def _combined_txt(job, sessions_iter, path):
    kinds = [k for k in ('recap', 'transcript') if k in job['content']]
    show_label = len(kinds) > 1
    bar = "=" * 60
    written = 0
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f"{job['campaign_name']}\nExported {datetime.now().strftime('%Y-%m-%d')}\n")
        for meta, data in sessions_iter:
            sections = [(k, data.get(k)) for k in kinds if data.get(k)]
            if not sections:
                _bump(job)
                continue
            f.write(f"\n{bar}\n{_session_title(meta).upper()} - {meta['date_pretty']}\n{bar}\n")
            for kind, text in sections:
                if show_label:
                    f.write(f"\n[{kind.upper()}]\n")
                f.write("\n")
                body = clean_recap(text) if kind == 'recap' else "\n".join(
                    l.rstrip() for l in text.split('\n') if l.strip())
                f.write(body + "\n")
            written += 1
            _bump(job)
    if not written:
        raise NothingToExport("None of the selected sessions have the selected content.")


def _build_zip(job, sessions_iter, path):
    root = safe_name(job['campaign_name'])
    used = set()
    written = 0
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for meta, data in sessions_iter:
            files = []
            if 'transcript' in job['content'] and data.get('transcript'):
                files.append(('transcript.txt', data['transcript']))
            if 'recap' in job['content'] and data.get('recap'):
                files.append(('recap.md', clean_recap(data['recap'])))
            if 'players' in job['content']:
                names = set()
                for username, content in data.get('players', []):
                    if not content:
                        continue
                    base = safe_name(username, 'player')
                    name, n = base, 2
                    while name.lower() in names:
                        name, n = f"{base}_{n}", n + 1
                    names.add(name.lower())
                    files.append((f"players/{name}.txt", content))
            if not files:
                _bump(job)
                continue
            folder = f"Session_{meta['number']:02d}_{meta['date_iso']}"
            if folder in used:
                folder += f"_id{meta['id']}"
            used.add(folder)
            for name, content in files:
                z.writestr(f"{root}/{folder}/{name}", content)
            written += 1
            _bump(job)
    if not written:
        raise NothingToExport("None of the selected sessions have the selected content.")


# ───────────────────────────── data loading ─────────────────────────────

def _iter_sessions(job):
    """Yield (meta, data) one session at a time, in chronological order (needs app context)."""
    from sqlalchemy import func
    from database import db
    from models import Session, Transcript

    wanted = set(job['session_ids'])
    rows = (db.session.query(Session.id, Session.session_number, Session.session_date, Session.local_time_str)
            .filter(Session.campaign_id == job['campaign_id'])
            .order_by(Session.session_number.asc(), Session.session_date.asc(), Session.id.asc())
            .all())
    need_transcript = 'transcript' in job['content']
    need_recap = 'recap' in job['content']
    need_players = 'players' in job['content']

    for r in rows:
        if r.id not in wanted:
            continue
        if job['cancel']:
            raise ExportCancelled()
        iso, pretty = _date_labels(r.local_time_str, r.session_date)
        meta = {'id': r.id, 'number': r.session_number or 0, 'date_iso': iso, 'date_pretty': pretty}
        data = {}
        if need_transcript or need_recap:
            t, s = db.session.query(Session.transcript_text, Session.summary_text).filter(Session.id == r.id).one()
            if need_transcript:
                data['transcript'] = t
            if need_recap:
                data['recap'] = s
        if need_players:
            data['players'] = [(p.username, p.content) for p in
                               db.session.query(Transcript.username, Transcript.content)
                               .filter(Transcript.session_id == r.id).order_by(Transcript.username.asc()).all()]
        yield meta, data


# ───────────────────────────── job management ─────────────────────────────

def _update(job, **kw):
    with _lock:
        job.update(kw)


def _bump(job):
    with _lock:
        job['done'] += 1


def _cleanup():
    """Remove expired exports (files and registry entries), and stray files from earlier runs."""
    now = time.time()
    with _lock:
        stale = [jid for jid, j in _jobs.items()
                 if j['finished'] and now - j['finished'] > TTL_SECONDS]
        for jid in stale:
            j = _jobs.pop(jid)
            if j.get('path'):
                try:
                    os.remove(j['path'])
                except OSError:
                    pass
        known = {os.path.basename(j['path']) for j in _jobs.values() if j.get('path')}
    try:
        for name in os.listdir(EXPORT_DIR):
            p = os.path.join(EXPORT_DIR, name)
            if name not in known and now - os.path.getmtime(p) > TTL_SECONDS:
                os.remove(p)
    except OSError:
        pass


def _filename(job, all_selected, numbers):
    labels = []
    if 'transcript' in job['content']:
        labels.append('transcript')
    if 'recap' in job['content']:
        labels.append('recap')
    if 'players' in job['content']:
        labels.append('players')
    parts = [safe_name(job['campaign_name']), "_".join(labels) or 'export']
    if not all_selected and numbers:
        nums = sorted(numbers)
        if nums == list(range(nums[0], nums[-1] + 1)):
            parts.append(f"S{nums[0]}-S{nums[-1]}" if len(nums) > 1 else f"S{nums[0]}")
        else:
            parts.append(f"{len(nums)}sessions")
    parts.append(datetime.now().strftime('%Y-%m-%d'))
    return "_".join(parts) + "." + FORMATS[job['format']]


def _execute(app, job):
    """Run one export to completion. Updates job in place; never raises."""
    from sqlalchemy import func, or_
    from database import db
    from models import Session, Transcript

    _update(job, status='running', phase='Starting…')
    path = os.path.join(EXPORT_DIR, f"{job['id']}.{FORMATS[job['format']]}")
    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        with app.app_context():
            # "Everything" means every session that has the selected content, not every session.
            has_content = []
            if 'transcript' in job['content']:
                has_content.append(func.coalesce(func.length(Session.transcript_text), 0) > 0)
            if 'recap' in job['content']:
                has_content.append(func.coalesce(func.length(Session.summary_text), 0) > 0)
            if 'players' in job['content']:
                has_content.append(Session.id.in_(db.session.query(Transcript.session_id)))
            all_ids = {r.id for r in db.session.query(Session.id).filter(
                Session.campaign_id == job['campaign_id'], or_(*has_content))}
            numbers = [r.session_number or 0 for r in db.session.query(Session.session_number)
                       .filter(Session.id.in_(job['session_ids']))]
            all_selected = set(job['session_ids']) >= all_ids
            _update(job, total=len(job['session_ids']), done=0, phase='Building…')

            gen = _iter_sessions(job)
            fmt = job['format']
            if fmt == 'pdf':
                _build_pdf(job, gen, path)
            elif fmt == 'md':
                _combined_md(job, gen, path)
            elif fmt == 'txt':
                _combined_txt(job, gen, path)
            else:
                _build_zip(job, gen, path)
        _update(job, status='done', phase='Done', path=path, size=os.path.getsize(path),
                filename=_filename(job, all_selected, numbers), done=job['total'], finished=time.time())
    except ExportCancelled:
        _discard(path)
        _update(job, status='cancelled', phase='Cancelled', finished=time.time())
    except NothingToExport as e:
        _discard(path)
        _update(job, status='error', phase='Nothing to export', error=str(e), finished=time.time())
    except Exception as e:
        logging.exception("Campaign export failed")
        _discard(path)
        _update(job, status='error', phase='Failed', error=str(e) or e.__class__.__name__, finished=time.time())


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _thread_main(app, job):
    with _run_slot:
        if job['cancel']:
            _update(job, status='cancelled', phase='Cancelled', finished=time.time())
            return
        _execute(app, job)


def _new_job(campaign_id, campaign_name, session_ids, content, fmt):
    return {
        'id': uuid.uuid4().hex, 'campaign_id': campaign_id, 'campaign_name': campaign_name,
        'session_ids': list(session_ids), 'content': list(content), 'format': fmt,
        'status': 'queued', 'phase': 'Queued…', 'done': 0, 'total': len(session_ids),
        'cancel': False, 'created': time.time(), 'finished': None,
        'path': None, 'filename': None, 'size': 0, 'error': None,
    }


def start_export(app, campaign_id, campaign_name, session_ids, content, fmt):
    """Start a background export and return its id."""
    _cleanup()
    job = _new_job(campaign_id, campaign_name, session_ids, content, fmt)
    with _lock:
        _jobs[job['id']] = job
    threading.Thread(target=_thread_main, args=(app, job), daemon=True, name=f"export-{job['id'][:6]}").start()
    return job['id']


def run_sync(app, campaign_id, campaign_name, session_ids, content, fmt):
    """Run an export in the calling thread (used by the legacy direct-download URL)."""
    _cleanup()
    job = _new_job(campaign_id, campaign_name, session_ids, content, fmt)
    with _lock:
        _jobs[job['id']] = job
    _thread_main(app, job)
    return snapshot(job)


def get_job(export_id):
    with _lock:
        return _jobs.get(export_id)


def cancel(export_id):
    job = get_job(export_id)
    if job and job['status'] in ('queued', 'running'):
        job['cancel'] = True
        return True
    return False


def snapshot(job):
    """Public, JSON-safe view of a job."""
    with _lock:
        total = max(job['total'], 1)
        # Leave the last few percent for the (fast) merge/write step.
        pct = 100 if job['status'] == 'done' else min(95, int(job['done'] * 95 / total))
        return {
            'id': job['id'], 'campaign_id': job['campaign_id'], 'status': job['status'],
            'phase': job['phase'], 'done': job['done'], 'total': job['total'], 'percent': pct,
            'format': job['format'], 'content': job['content'], 'sessions': len(job['session_ids']),
            'filename': job['filename'], 'size': job['size'], 'error': job['error'],
            'created': job['created'],
        }


def jobs_for_campaign(campaign_id, limit=5):
    with _lock:
        mine = [j for j in _jobs.values() if j['campaign_id'] == campaign_id]
    mine.sort(key=lambda j: j['created'], reverse=True)
    return [snapshot(j) for j in mine[:limit]]
