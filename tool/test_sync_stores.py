"""Offline tests for sync_stores.py (no network, no git).

    python -m unittest discover -s tool -p "test_*.py"
"""
import contextlib
import datetime as dt
import html
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import sync_stores

PREFIX = 'com.crazypenguin.'
RAW = 'https://raw.githubusercontent.com/XPersPective/napp_apps/HEAD/icons/'
PNG = b'\x89PNG\r\n\x1a\n'
ICONS = {'doctorKey': PNG + b'doctorfilter', 'shopKey': PNG + b'nshoptor'}
LOCALES = ['en', 'tr'] + [f'x{i:02d}' for i in range(69)]    # 71, like DoctorFilter


def card(package, icon_key, title):
    """One app card as Google Play renders it on the developer page."""
    title = html.escape(title)
    lh = f'https://play-lh.googleusercontent.com/{icon_key}'
    return (f'<div role="listitem"><a class="Si6A0c" href="/store/apps/details?id={package}">'
            f'<div><img src="{lh}=s256" srcset="{lh}=s512 2x" alt="Icon image {title}" /></div>'
            f'<div><div title="{title}"><div>{title}</div></div></div></a></div>')


def developer_page(*cards):
    # Banner image and featured-app link without card content, as on Play.
    return ('<html><body><img src="https://play-lh.googleusercontent.com/banner=w3840-h2160" />'
            f'<a href="/store/apps/details?id={PREFIX}doctorfilter" aria-label="DoctorFilter"></a>'
            + ''.join(cards) + '</body></html>').encode()


def details_page(title, summary):
    return (f'<html><head><meta name="description" property="og:description" '
            f'content="{html.escape(summary)}"></head><body><h1><span itemprop="name">'
            f'{html.escape(title)}</span></h1></body></html>').encode()


PAGES = {
    'dev:en': developer_page(card(PREFIX + 'doctorfilter', 'doctorKey', 'DoctorFilter Blue Light Filter'),
                             card(PREFIX + 'nshoptor', 'shopKey', 'NShoptor: Smart Grocery List'),
                             card('com.other.app', 'otherKey', 'Not one of ours')),
    'dev:tr': developer_page(card(PREFIX + 'doctorfilter', 'doctorKey', 'DoctorFilter: Mavi Işık Filtre'),
                             card(PREFIX + 'nshoptor', 'shopKey', 'NShoptor: Alışveriş Listesi')),
    'details:en': details_page('NShoptor: Smart Grocery List', 'Grocery list & budget: planned vs actual.'),
    'details:tr': details_page('NShoptor: Alışveriş Listesi', 'Alışveriş listesi ve bütçe: tahmini/gerçek.'),
}
ARTIST = {'wrapperType': 'artist', 'artistId': 6821332438, 'artistName': 'Nevzat Arslan'}
KAMUBUL_IOS = {'wrapperType': 'software', 'kind': 'software', 'trackId': 6826592263,
               'bundleId': PREFIX + 'kamubul'}


def catalogue():
    return {'schema': 1, 'apps': [
        {'id': 'doctorfilter', 'androidPackage': PREFIX + 'doctorfilter',
         'icon': RAW + 'doctorfilter.png',
         'name': {code: f'DoctorFilter {code}' for code in LOCALES},
         'description': {code: f'Blue light filter {code}' for code in LOCALES}, 'order': 1},
        {'id': 'kamubul', 'androidPackage': PREFIX + 'kamubul', 'icon': RAW + 'kamubul.png',
         'name': {'en': 'KamuBul', 'tr': 'KamuBul'},
         'description': {'en': 'Public jobs.', 'tr': 'Kamu ilanları.'}, 'order': 3},
    ]}


class SyncStoresTest(unittest.TestCase):

    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = workspace.name
        os.mkdir(self.path('icons'))
        self.write('apps.json', sync_stores._dump(catalogue()).encode())
        self.write('icons/doctorfilter.png', PNG + b'old icon')
        self.write('icons/kamubul.png', PNG + b'kamubul')
        self.itunes_tr = {'resultCount': 2, 'results': [ARTIST, KAMUBUL_IOS]}
        self.play_down = False
        self.last_commit = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        for name, fake in (('fetch_url', self.fake_fetch),
                           ('last_commit_time', lambda root: self.last_commit)):
            patcher = mock.patch.object(sync_stores, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def path(self, name):
        return os.path.join(self.root, *name.split('/'))

    def read(self, name):
        with open(self.path(name), 'rb') as file:
            return file.read()

    def write(self, name, data):
        with open(self.path(name), 'wb') as file:
            file.write(data)

    def apps(self):
        return {app['id']: app for app in json.loads(self.read('apps.json'))['apps']}

    def fake_fetch(self, url):
        if url.startswith('https://play.google.com/store/apps/dev?'):
            if self.play_down:
                raise urllib.error.URLError('Google Play unreachable')
            return PAGES['dev:en' if 'hl=en' in url else 'dev:tr']
        if url.startswith(f'https://play.google.com/store/apps/details?id={PREFIX}nshoptor&'):
            return PAGES['details:en' if 'hl=en' in url else 'details:tr']
        if url.startswith('https://play-lh.googleusercontent.com/'):
            key, size = url.rsplit('/', 1)[1].split('=')
            self.assertEqual(size, 's256')
            return ICONS[key]
        if url.startswith('https://itunes.apple.com/lookup?'):
            data = self.itunes_tr if 'country=tr' in url else {'resultCount': 1, 'results': [ARTIST]}
            return json.dumps(data).encode()
        raise AssertionError(f'unexpected fetch: {url}')

    def run_main(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = sync_stores.main(['--root', self.root, *args])
        return code, out.getvalue()

    def test_adds_new_app_with_names_icon_and_raw_url(self):
        self.assertEqual(self.run_main()[0], 0)
        apps = self.apps()
        self.assertEqual(sorted(apps), ['doctorfilter', 'kamubul', 'nshoptor'])    # no com.other.app
        shop = apps['nshoptor']
        self.assertEqual(shop['androidPackage'], PREFIX + 'nshoptor')
        self.assertEqual(shop['name'], {'en': 'NShoptor: Smart Grocery List',
                                        'tr': 'NShoptor: Alışveriş Listesi'})
        self.assertEqual(shop['description'], {'en': 'Grocery list & budget: planned vs actual.',
                                               'tr': 'Alışveriş listesi ve bütçe: tahmini/gerçek.'})
        self.assertEqual(shop['icon'], RAW + 'nshoptor.png')
        self.assertEqual(shop['order'], 4)
        self.assertEqual(self.read('icons/nshoptor.png'), ICONS['shopKey'])
        self.assertEqual(json.loads(self.read('sync-report.json')), {'missingFromPlay': ['kamubul']})

    def test_preserves_existing_maps_order_and_icon_url(self):
        before = catalogue()['apps'][0]
        self.run_main()
        doctor = self.apps()['doctorfilter']
        self.assertEqual(len(doctor['name']), 71)
        for field in ('name', 'description', 'order', 'icon'):
            self.assertEqual(doctor[field], before[field], field)
        # An existing icon file is never replaced by the store render.
        self.assertEqual(self.read('icons/doctorfilter.png'), b'\x89PNG\r\n\x1a\nold icon')
        self.assertEqual([app['id'] for app in json.loads(self.read('apps.json'))['apps']],
                         ['doctorfilter', 'kamubul', 'nshoptor'])

    def test_joins_app_store_id_as_string(self):
        self.run_main()
        kamubul = self.apps()['kamubul']
        self.assertEqual(kamubul['appStoreId'], '6826592263')
        self.assertEqual(list(kamubul), ['id', 'androidPackage', 'appStoreId', 'icon', 'name',
                                         'description', 'order'])
        self.assertIn('"appStoreId": "6826592263"', self.read('apps.json').decode())

    def test_malformed_itunes_ids_are_ignored(self):
        self.itunes_tr['results'] += [
            {'wrapperType': 'software', 'bundleId': PREFIX + 'nshoptor', 'trackId': 1234.5},
            {'wrapperType': 'software', 'bundleId': PREFIX + 'doctorfilter', 'trackId': True},
            {'wrapperType': 'software', 'bundleId': PREFIX + 'doctorfilter', 'trackId': -7},
            {'wrapperType': 'software', 'bundleId': PREFIX + 'doctorfilter', 'trackId': '12ab'},
            {'wrapperType': 'software', 'trackId': 42},
            'not an object',
        ]
        self.assertEqual(self.run_main()[0], 0)
        apps = self.apps()
        self.assertNotIn('appStoreId', apps['nshoptor'])
        self.assertNotIn('appStoreId', apps['doctorfilter'])
        self.assertEqual(apps['kamubul']['appStoreId'], '6826592263')

    def test_play_failure_aborts_without_touching_catalogue(self):
        self.play_down = True
        before = self.read('apps.json')
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.read('apps.json'), before)
        self.assertFalse(os.path.exists(self.path('icons/nshoptor.png')))
        self.assertFalse(os.path.exists(self.path('sync-report.json')))

    def test_invalid_entry_blocks_the_write(self):
        broken = catalogue()
        broken['apps'][1]['icon'] = 'http://example.com/kamubul.png'
        self.write('apps.json', sync_stores._dump(broken).encode())
        before = self.read('apps.json')
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.read('apps.json'), before)

    def test_second_run_is_idempotent(self):
        self.assertEqual(self.run_main()[0], 0)
        # A Windows checkout with core.autocrlf holds CRLF; that is no change either.
        self.write('apps.json', self.read('apps.json').replace(b'\n', b'\r\n'))
        files = ('apps.json', 'sync-report.json', 'icons/doctorfilter.png', 'icons/nshoptor.png')
        snapshot = {name: self.read(name) for name in files}
        summary = sync_stores.sync(self.root)
        self.assertFalse(summary['changed'])
        self.assertEqual(summary['icons'], [])
        self.assertEqual(summary['added'], [])
        for name in files:
            self.assertEqual(self.read(name), snapshot[name], name)

    def test_dry_run_writes_nothing(self):
        before = self.read('apps.json')
        code, out = self.run_main('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Adds: nshoptor', out)
        self.assertIn('iOS joins: kamubul=6826592263 (new)', out)
        self.assertEqual(self.read('apps.json'), before)
        self.assertEqual(self.read('icons/doctorfilter.png'), b'\x89PNG\r\n\x1a\nold icon')
        self.assertFalse(os.path.exists(self.path('icons/nshoptor.png')))
        self.assertFalse(os.path.exists(self.path('sync-report.json')))

    def test_keepalive_written_only_after_45_days(self):
        self.run_main()
        self.assertFalse(os.path.exists(self.path('.last-sync')))
        self.last_commit = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=46)
        self.run_main()
        self.assertEqual(self.read('.last-sync').decode(),
                         dt.datetime.now(dt.timezone.utc).date().isoformat() + '\n')

    def test_validate_entry_rejects_broken_entry(self):
        good = catalogue()['apps'][1]
        self.assertEqual(sync_stores.validate_entry(good), [])
        broken = dict(good, appStoreId=6826592263, icon='http://example.com/kamubul.png')
        del broken['description']
        problems = ' | '.join(sync_stores.validate_entry(broken))
        for field in ('description', 'appStoreId', 'icon'):
            self.assertIn(field, problems)
        for change in ({'appStoreId': 6826592263}, {'icon': 'http://example.com/i.png'},
                       {'description': {'en': ' '}}, {'name': {}}, {'id': ' '}, {'order': '1'}):
            self.assertTrue(sync_stores.validate_entry(dict(good, **change)), change)
        ios_only = {'id': 'x', 'appStoreId': '1', 'name': {'en': 'X'}, 'description': {'en': 'X'}}
        self.assertEqual(sync_stores.validate_entry(ios_only), [])
        self.assertTrue(sync_stores.validate_entry(dict(ios_only, appStoreId=None)))


if __name__ == '__main__':
    unittest.main()
