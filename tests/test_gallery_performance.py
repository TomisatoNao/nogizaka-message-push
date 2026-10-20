"""Statistics equivalence and first-page scheduling regressions."""
import concurrent.futures
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time

import pytest

from src.webui_modules.archive import gallery as gm


def test_materialized_counts_preserve_body_validation():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE blog_posts(group_key,author,date,images_json,image_paths_json,body_html)')
    rows = [
        ('hinatazaka', 'A', '2022-01-01', ['https://cdn/a.jpg', 'https://cdn/wrong.jpg'], [], '<img src="https://cdn/a.jpg">'),
        ('hinatazaka46', 'A', '2022-01-02', ['http://cdn/b.jpg?a=1&b=2'], [], '<img src="https://CDN/b.jpg?a=1&amp;b=2">'),
        ('sakurazaka', 'B', '2023-01-01', ['https://cdn/c.jpg'], [], '<img src="different">'),
        ('hinatazaka', 'C', '2023-01-01', [], ['local/a.jpg'], '<img src="different">'),
        ('hinatazaka', 'D', '2024-01-01', ['https://cdn/d.jpg'], [], ''),
        ('hinatazaka', 'E', '2024-01-01', ['https://cdn/e.jpg', '', None], [], 'no images'),
    ]
    db.executemany('INSERT INTO blog_posts VALUES(?,?,?,?,?,?)', [
        (g, a, d, json.dumps(img), json.dumps(paths), body) for g, a, d, img, paths, body in rows
    ])
    cols = {r[1] for r in db.execute('PRAGMA table_info(blog_posts)')}
    where, target = gm._get_blog_image_expr(cols)
    original = f'''SELECT p.author, COUNT(*) FROM blog_posts p,json_each({target}) j
        WHERE {where} AND j.value IS NOT NULL AND j.value != ''
        AND {gm._get_blog_image_body_filter(cols)} GROUP BY p.author ORDER BY p.author'''
    cte, body_filter = gm._blog_count_source(cols, where)
    optimized = cte + f'''SELECT p.author, COUNT(*) FROM gallery_posts p,json_each(p.gallery_images) j
        WHERE j.value IS NOT NULL AND j.value != '' AND {body_filter}
        GROUP BY p.author ORDER BY p.author'''
    assert db.execute(original).fetchall() == db.execute(optimized).fetchall()
    assert dict(db.execute(optimized)) == {'A': 2, 'B': 1, 'C': 1, 'D': 1, 'E': 1}
    db.close()


def test_year_statistics_cold_requests_are_coalesced(monkeypatch):
    monkeypatch.setattr(gm, '_gallery_years_cache', {})
    monkeypatch.setattr(gm, '_gallery_total_cache', {})
    calls = []
    barrier = threading.Barrier(6)

    def fetch(member_dir):
        calls.append(member_dir)
        time.sleep(.04)
        return {2026: 3}

    monkeypatch.setattr(gm._archive, 'get_gallery_message_years', fetch)

    def request(_):
        barrier.wait(timeout=5)
        return gm.get_gallery_years('member', 'message')

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(request, range(6)))
    assert calls == ['member']
    assert all(row['total'] == 3 for row in results)
    # Keep the original five-minute expiry, rather than hiding changes forever.
    stamp, value = gm._gallery_years_cache[('member', 'message')]
    gm._gallery_years_cache[('member', 'message')] = (stamp - 301, value)
    assert gm.get_gallery_years('member', 'message')['total'] == 3
    assert calls == ['member', 'member']


def test_member_statistics_cold_requests_are_coalesced(monkeypatch):
    from src import avatar_manager
    from src.webui_modules import archive_handlers

    monkeypatch.setattr(gm, '_gallery_members_cache', None)
    monkeypatch.setattr(gm._archive, 'init_db', lambda: None)
    monkeypatch.setattr(archive_handlers, 'get_blog_db', lambda: None)
    calls = []
    barrier = threading.Barrier(6)

    def avatars():
        calls.append(1)
        time.sleep(.04)
        return {}

    monkeypatch.setattr(avatar_manager, 'get_member_avatar_map', avatars)

    def request(_):
        barrier.wait(timeout=5)
        return gm.get_gallery_members()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(request, range(6)))
    assert calls == [1]
    assert all(result == {'ok': True, 'members': []} for result in results)


def test_old_sqlite_fallback_still_checks_article_ownership(monkeypatch):
    from src.webui_modules import archive_handlers

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE blog_posts(group_key,author,date,images_json,image_paths_json,body_html)')
    db.execute('INSERT INTO blog_posts VALUES(?,?,?,?,?,?)', (
        'hinatazaka', 'A', '2022-01-01', json.dumps(['https://cdn/good.jpg', 'https://cdn/wrong.jpg']),
        '[]', '<img src="https://cdn/good.jpg">',
    ))

    class OldSQLite:
        def execute(self, sql, *args):
            if sql.lstrip().startswith('WITH gallery_posts AS MATERIALIZED'):
                raise sqlite3.OperationalError('near MATERIALIZED: syntax error')
            return db.execute(sql, *args)

    monkeypatch.setattr(archive_handlers, 'get_blog_db', lambda: OldSQLite())
    monkeypatch.setattr(gm, '_gallery_years_cache', {})
    monkeypatch.setattr(gm, '_gallery_total_cache', {})
    assert gm.get_gallery_years(source='blog')['years'] == [{'year': 2022, 'count': 1}]
    db.close()


def test_gallery_selection_starts_unfiltered_photos_before_metadata():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for JS scheduling regression')
    js = (Path(__file__).parents[1] / 'src/webui_static/archive.js').read_text(encoding='utf-8')
    fn = js[js.index('async function selectGalleryMember'):js.index('function getGalleryGridCols')]
    script = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const fn = JSON.parse(process.argv[1]);
async function scenario(year, stale = false) {
 const calls = [];
 let finishYears;
 const elem = () => ({style:{},classList:{remove(){}},closest(){return null}});
 const context = {
  gallerySelectionVersion:0,curMode:'',curGalleryMember:'',curGalleryYear:year,
  galleryMembers:[{}],localStorage:{setItem(){}},$:elem,
  document:{querySelector:elem},hideMessageMonthFooter(){},setHtmlViewClass(){},
  updateGalleryMemberButtonDisplay(){},syncGallerySourceChips(){},syncGallerySortButton(){},
  syncGalleryLayoutToggle(){},syncNavTabs(){},renderGalleryMemberPopover(){},
  syncGalleryHash(){calls.push('hash')},
  loadGalleryPhotos(){calls.push('photos'); return Promise.resolve()},
  loadGalleryYears(){calls.push('years');return new Promise(r => {finishYears=r})},
 };
 vm.createContext(context); vm.runInContext(fn,context);
 const pending = context.selectGalleryMember('member');
 assert.deepEqual(calls, year ? ['years'] : ['photos','years']);
 if (stale) context.gallerySelectionVersion++;
 finishYears(); await pending;
 assert.deepEqual(calls, stale ? (year ? ['years'] : ['photos','years'])
   : (year ? ['years','hash','photos'] : ['photos','years','hash']));
}
(async()=>{await scenario('');await scenario('2022');await scenario('2022',true);await scenario('',true)})()
 .catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', script, json.dumps(fn)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
