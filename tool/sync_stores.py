#!/usr/bin/env python3
"""Keeps apps.json in step with the public Google Play and App Store listings.

Run every day by .github/workflows/sync-stores.yml; safe to run by hand:

    python3 tool/sync_stores.py [--dry-run]

Sources (public, no credentials):
  * Google Play developer page, en-US and tr-TR: packages, titles and icons.
  * Google Play details pages, for new apps only: title and short description.
  * iTunes artist lookup (tr, us, de, gb): App Store ids, joined to entries
    by bundleId == androidPackage.

Merge rules: existing fields are kept (translations, order, icon URL), except
that appStoreId follows the live lookup; apps new on Google Play are appended;
entries are never deleted -- ids missing from Google Play go to
sync-report.json. Everything is fetched, merged and validated against the
napp_core consumer contract in memory first, so any error exits with status 1
before apps.json is touched.
"""
import argparse
import datetime as dt
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DEVELOPER_ID = '9121277038774513673'      # Google Play developer page
ARTIST_ID = '6821332438'                  # App Store artist
PACKAGE_PREFIX = 'com.crazypenguin.'      # only our own apps are catalogued
PLAY_LOCALES = (('en', 'US'), ('tr', 'TR'))
ITUNES_COUNTRIES = ('tr', 'us', 'de', 'gb')
# Catalogue ids that are not simply the package name without PACKAGE_PREFIX.
ID_OVERRIDES = {'com.crazypenguin.nketotracker': 'nketo'}

DEVELOPER_URL = 'https://play.google.com/store/apps/dev?id={developer}&hl={hl}&gl={gl}'
DETAILS_URL = 'https://play.google.com/store/apps/details?id={package}&hl={hl}&gl={gl}'
LOOKUP_URL = ('https://itunes.apple.com/lookup?id={artist}&entity=software'
              '&limit=200&country={country}')
ICON_URL = 'https://raw.githubusercontent.com/XPersPective/napp_apps/HEAD/icons/{id}.png'
ICON_SIZE = '=s256'                       # play-lh suffix: 256 px square PNG

CATALOGUE, REPORT, KEEPALIVE = 'apps.json', 'sync-report.json', '.last-sync'
MAX_BYTES = 256 * 1024                    # the apps refuse larger catalogues
MAX_DESCRIPTION = 90
KEEPALIVE_AFTER = dt.timedelta(days=45)   # GitHub disables schedules idle for 60 days
FIELD_ORDER = ('id', 'androidPackage', 'appStoreId', 'icon', 'name', 'description', 'order')
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
TIMEOUT = 30
USER_AGENT = 'Mozilla/5.0'

_ICON_SRC = re.compile(r'(https://play-lh\.googleusercontent\.com/[^=\s"]+)=s\d+')
_PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+')
_DIGITS = re.compile(r'[0-9]+')
_FILE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')


class SyncError(Exception):
    """A source or the merged catalogue is unusable; apps.json stays as it is."""


# --- Network and git access (module level so tests can replace them) --------

def fetch_url(url: str) -> bytes:
    """Body of a GET request; raises OSError/HTTPException on failure."""
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def fetch_json(url: str):
    return json.loads(fetch_url(url).decode('utf-8'))


def last_commit_time(root: str) -> dt.datetime | None:
    """Committer time of HEAD, or None when git or the checkout is missing."""
    try:
        result = subprocess.run(['git', '-C', root, 'log', '-1', '--format=%ct'],
                                capture_output=True, text=True, timeout=TIMEOUT, check=True)
        return dt.datetime.fromtimestamp(int(result.stdout.strip()), dt.timezone.utc)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _fetch(fetcher, url: str):
    """Calls fetch_url/fetch_json, naming the URL in any error."""
    try:
        return fetcher(url)
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise SyncError(f'{url}: {error}') from error


# --- Google Play -------------------------------------------------------------

def _details_package(href: str) -> str | None:
    """Our package named by a /store/apps/details?id=... link, else None."""
    parts = urllib.parse.urlsplit(href)
    if not parts.path.endswith('/store/apps/details'):
        return None
    package = urllib.parse.parse_qs(parts.query).get('id', [''])[0]
    return package if package.startswith(PACKAGE_PREFIX) and _PACKAGE.fullmatch(package) else None


class _DeveloperPage(HTMLParser):
    """Collects the app cards of a developer page as {package: {title, icon}}.

    A card is an <a href=".../details?id=PKG"> holding a square icon
    <img src="https://play-lh.googleusercontent.com/KEY=sNN"> and an element
    with title="App title". Links without both (the featured-app banner) are
    skipped; the img alt text is localized ('Simge resmi ...'), so it is unused.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: dict[str, dict] = {}
        self._open: dict | None = None    # card inside the <a> being read

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            package = _details_package(attrs.get('href') or '')
            self._open = {'package': package, 'title': '', 'icon': ''} if package else None
        elif self._open is not None:
            icon = _ICON_SRC.fullmatch(attrs.get('src') or '') if tag == 'img' else None
            if icon and not self._open['icon']:
                self._open['icon'] = icon.group(1)    # base URL, size suffix dropped
            title = ' '.join((attrs.get('title') or '').split())
            if title and not self._open['title']:
                self._open['title'] = title

    def handle_endtag(self, tag):
        if tag != 'a' or self._open is None:
            return
        card, self._open = self._open, None
        if card['title'] and card['icon']:
            self.cards.setdefault(card.pop('package'), card)


class _DetailsPage(HTMLParser):
    """Reads the title (<h1>) and short description (<meta name=description>)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ''
        self.summary = ''
        self._h1: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'description' and not self.summary:
            self.summary = ' '.join((attrs.get('content') or '').split())
        elif tag == 'h1' and not self.title:
            self._h1 = []

    def handle_data(self, data):
        if self._h1 is not None:
            self._h1.append(data)

    def handle_endtag(self, tag):
        if tag == 'h1' and self._h1 is not None:
            self.title = ' '.join(''.join(self._h1).split())
            self._h1 = None


def read_play() -> tuple[dict, dict]:
    """Our apps on the developer pages of every PLAY_LOCALES country.

    Returns ({package: {'titles': {hl: title}, 'icon': base URL, 'gl': country
    it was first seen in}}, {'en-US': [packages], ...}). The union is used
    because some apps are published in one country only.
    """
    apps, found = {}, {}
    for hl, gl in PLAY_LOCALES:
        url = DEVELOPER_URL.format(developer=DEVELOPER_ID, hl=hl, gl=gl)
        page = _DeveloperPage()
        page.feed(_fetch(fetch_url, url).decode('utf-8'))
        page.close()
        if not page.cards:
            raise SyncError(f'{url}: no app cards found (page layout changed?)')
        for package, card in page.cards.items():
            app = apps.setdefault(package, {'titles': {}, 'icon': card['icon'], 'gl': gl})
            app['titles'][hl] = card['title']
        found[f'{hl}-{gl}'] = sorted(page.cards)
    return apps, found


def _shorten(text: str) -> str:
    """Caps a description at MAX_DESCRIPTION characters on a word boundary."""
    if len(text) <= MAX_DESCRIPTION:
        return text
    return text[:MAX_DESCRIPTION - 1].rsplit(' ', 1)[0].rstrip(' ,;:-') + '…'


def read_details(package: str, app: dict) -> dict:
    """en/tr name and description maps of a new app.

    The title comes from the developer page when the app is listed in that
    locale's country (else from the details page); the description is the
    store short description. A locale whose country does not list the app is
    read from the country it was found in.
    """
    names, descriptions = {}, {}
    for hl, gl in PLAY_LOCALES:
        url = DETAILS_URL.format(package=package, hl=hl,
                                 gl=gl if hl in app['titles'] else app['gl'])
        page = _DetailsPage()
        page.feed(_fetch(fetch_url, url).decode('utf-8'))
        page.close()
        names[hl] = app['titles'].get(hl) or page.title
        descriptions[hl] = _shorten(page.summary)
        if not names[hl] or not descriptions[hl]:
            raise SyncError(f'{url}: no title or short description found')
    return {'name': names, 'description': descriptions}


# --- App Store ---------------------------------------------------------------

def read_app_store() -> tuple[dict, dict]:
    """({bundleId: trackId as a digit string}, {country: [bundleIds]}).

    iTunes sends trackId as a JSON number, but the apps drop entries whose
    appStoreId is not a string, so ids are stored as strings; results with a
    malformed id (float, bool, negative, non-digit) or no bundleId are skipped.
    """
    ids, found = {}, {}
    for country in ITUNES_COUNTRIES:
        url = LOOKUP_URL.format(artist=ARTIST_ID, country=country)
        data = _fetch(fetch_json, url)
        results = data.get('results') if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise SyncError(f'{url}: no results list in the response')
        found[country] = []
        for item in results:
            if not isinstance(item, dict) or item.get('wrapperType') != 'software':
                continue
            bundle, track = item.get('bundleId'), item.get('trackId')
            if isinstance(track, bool) or not isinstance(track, (int, str)):
                continue
            if isinstance(bundle, str) and _DIGITS.fullmatch(str(track)):
                ids.setdefault(bundle, str(track))
                found[country].append(bundle)
    return ids, found


# --- Consumer contract (napp_core parser) ------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text_map(value) -> bool:
    return isinstance(value, dict) and bool(value) and all(
        isinstance(key, str) and key.strip() and isinstance(text, str) and text.strip()
        for key, text in value.items())


def validate_entry(entry) -> list[str]:
    """Reasons the napp_core parser would drop this entry; [] when it is valid."""
    if not isinstance(entry, dict):
        return ['entry is not an object']
    problems = []
    if not isinstance(entry.get('id'), str) or not entry['id'].strip():
        problems.append('id must be a non-blank string')
    package, store_id = entry.get('androidPackage'), entry.get('appStoreId')
    if package is None and store_id is None:
        problems.append('needs androidPackage and/or appStoreId')
    if package is not None and (not isinstance(package, str) or not package.strip()):
        problems.append('androidPackage must be a non-blank string')
    if store_id is not None and not (isinstance(store_id, str) and _DIGITS.fullmatch(store_id)):
        problems.append('appStoreId must be a string of digits')
    icon = entry.get('icon')
    if icon is not None:
        parts = urllib.parse.urlsplit(icon) if isinstance(icon, str) else None
        if parts is None or parts.scheme != 'https' or not parts.netloc:
            problems.append('icon must be an https URL')
    for field in ('name', 'description'):
        if not _is_text_map(entry.get(field)):
            problems.append(f'{field} must map locales to non-blank text')
    if not _is_int(entry.get('order', 0)):
        problems.append('order must be an integer')
    return problems


def _contract_problems(catalogue: dict) -> list[str]:
    apps = catalogue['apps']
    problems = [f'{app.get("id")!r}: {problem}' for app in apps for problem in validate_entry(app)]
    if not _is_int(catalogue.get('schema')) or catalogue['schema'] != 1:
        problems.append('schema must be 1')
    if not problems:
        ids = [app['id'] for app in apps]
        problems += [f'duplicate id {i!r}' for i in sorted(set(ids)) if ids.count(i) > 1]
    return problems


# --- Merge and write ---------------------------------------------------------

def _put(entry: dict, key: str, value) -> None:
    """Sets entry[key] in place; a new key goes to its README position."""
    if key in entry:
        entry[key] = value
        return
    later = set(FIELD_ORDER[FIELD_ORDER.index(key) + 1:])
    items = list(entry.items())
    entry.clear()
    for name, current in items:
        if key not in entry and name in later:
            entry[key] = value
        entry[name] = current
    entry.setdefault(key, value)


def _base_id(package: str) -> str:
    """Catalogue id of a new package: its name after PACKAGE_PREFIX."""
    return ID_OVERRIDES.get(package) or re.sub(
        r'[^a-z0-9_]', '_', package[len(PACKAGE_PREFIX):].lower())


def _read(path: str) -> bytes:
    with open(path, 'rb') as file:
        return file.read()


def _write(path: str, data: bytes) -> None:
    """Replaces path atomically, so readers never see a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + '.tmp'
    with open(temporary, 'wb') as file:
        file.write(data)
    os.replace(temporary, path)


def _same_text(path: str, text: str) -> bool:
    """True when path holds text; CRLF checkouts (core.autocrlf) count as equal."""
    return os.path.isfile(path) and _read(path).replace(b'\r\n', b'\n') == text.encode('utf-8')


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def sync(root: str, dry_run: bool = False) -> dict:
    """Runs one sync and returns its summary.

    All sources are fetched, merged and validated in memory before the first
    write, so a raised error leaves the checkout untouched. A dry run stops
    before writing.
    """
    catalogue_path = os.path.join(root, CATALOGUE)
    old_text = _read(catalogue_path).decode('utf-8').replace('\r\n', '\n')
    catalogue = json.loads(old_text)
    if not (isinstance(catalogue, dict) and isinstance(catalogue.get('apps'), list)
            and all(isinstance(app, dict) for app in catalogue['apps'])):
        raise SyncError(f'{CATALOGUE} is not {{"schema": 1, "apps": [objects]}}')
    apps = catalogue['apps']

    play, play_found = read_play()
    store_ids, store_found = read_app_store()

    by_package = {}
    for app in apps:
        if isinstance(app.get('androidPackage'), str):
            by_package.setdefault(app['androidPackage'], app)

    # Apps new on Google Play: appended after the highest existing order.
    taken = {app['id'] for app in apps if isinstance(app.get('id'), str)}
    next_order = max((app.get('order', 0) for app in apps if _is_int(app.get('order', 0))),
                     default=0) + 1
    added = []
    for package in sorted(set(play) - set(by_package), key=_base_id):
        app_id = _base_id(package)
        if app_id in taken:
            app_id = re.sub(r'[^a-z0-9_]', '_', package.lower())
        taken.add(app_id)
        entry = {'id': app_id, 'androidPackage': package, 'icon': ICON_URL.format(id=app_id),
                 **read_details(package, play[package]), 'order': next_order}
        next_order += 1
        apps.append(entry)
        by_package[package] = entry
        added.append(entry)

    # An icon file is written only when it does not exist yet: the checked-in
    # icons are hand-tuned (transparent corners) and are never replaced by the
    # store render. New apps get the store icon on first sync.
    icon_writes = {}
    for package in sorted(play):
        entry = by_package[package]
        if not isinstance(entry.get('id'), str) or not _FILE_ID.fullmatch(entry['id']):
            raise SyncError(f'{package}: id {entry.get("id")!r} cannot name an icon file')
        icon_path = os.path.join(root, 'icons', entry['id'] + '.png')
        if os.path.isfile(icon_path):
            continue
        data = _fetch(fetch_url, play[package]['icon'] + ICON_SIZE)
        if not data.startswith(PNG_SIGNATURE):
            raise SyncError(f'{package}: the Google Play icon is not a PNG')
        icon_writes[icon_path] = data
        _put(entry, 'icon', ICON_URL.format(id=entry['id']))

    # App Store ids follow the live lookup; they are never removed.
    joins = []
    for app in apps:
        package = app.get('androidPackage')
        store_id = store_ids.get(package) if isinstance(package, str) else None
        if store_id:
            joins.append((app.get('id'), store_id, app.get('appStoreId') != store_id))
            _put(app, 'appStoreId', store_id)
    unmatched_ios = sorted(bundle for bundle in store_ids if bundle not in by_package)
    missing = sorted(str(app.get('id')) for app in apps
                     if isinstance(app.get('androidPackage'), str)
                     and app['androidPackage'] not in play)

    problems = _contract_problems(catalogue)
    if problems:
        raise SyncError('result would break the app contract: ' + '; '.join(problems))
    apps.sort(key=lambda app: (app.get('order', 0), app['id']))
    new_text = _dump(catalogue)
    size = len(new_text.encode('utf-8'))
    if size >= MAX_BYTES:
        raise SyncError(f'{CATALOGUE} would be {size} bytes (limit {MAX_BYTES})')
    changed = new_text != old_text

    now = dt.datetime.now(dt.timezone.utc)
    last_commit = last_commit_time(root)
    keepalive = last_commit is not None and now - last_commit > KEEPALIVE_AFTER

    if not dry_run:
        # Icons first, so apps.json never names an icon that is not there yet.
        for icon_path, data in icon_writes.items():
            _write(icon_path, data)
        if changed:
            _write(catalogue_path, new_text.encode('utf-8'))
        report_path = os.path.join(root, REPORT)
        report = _dump({'missingFromPlay': missing})
        if missing and not _same_text(report_path, report):
            _write(report_path, report.encode('utf-8'))
        elif not missing and os.path.exists(report_path):
            os.remove(report_path)
        stamp = now.date().isoformat() + '\n'
        keepalive_path = os.path.join(root, KEEPALIVE)
        if keepalive and not _same_text(keepalive_path, stamp):
            _write(keepalive_path, stamp.encode('utf-8'))

    return {
        'play': play_found, 'app_store': store_found, 'added': added, 'joins': joins,
        'unmatched_ios': unmatched_ios, 'missing': missing,
        'icons_fetched': len(play), 'icons': sorted(os.path.basename(p) for p in icon_writes),
        'apps': len(apps), 'old_size': len(old_text.encode('utf-8')), 'size': size,
        'changed': changed, 'keepalive': keepalive,
        'last_commit_days': (now - last_commit).days if last_commit else None,
    }


# --- Command line ------------------------------------------------------------

def _annotate(kind: str, message: str) -> None:
    """Prints to stderr; GitHub Actions shows '::warning::' lines on the run."""
    prefix = f'::{kind}::' if os.environ.get('GITHUB_ACTIONS') == 'true' else f'{kind}: '
    print(prefix + message, file=sys.stderr)


def _listing(names: list[str]) -> str:
    short = [name.removeprefix(PACKAGE_PREFIX) for name in names]
    text = f'{len(names)} app' + ('' if len(names) == 1 else 's')
    return text + (f' ({", ".join(short)})' if short else '')


def _print_summary(summary: dict, dry_run: bool) -> None:
    for source, packages in summary['play'].items():
        print(f'Google Play {source}: {_listing(packages)}')
    for country, bundles in summary['app_store'].items():
        print(f'App Store {country}: {_listing(bundles)}')
    adds = [f"{app['id']} ({app['androidPackage']}, order {app['order']}, "
            f"en: {app['name']['en']})" for app in summary['added']]
    print('Adds:', '; '.join(adds) or 'none')
    joins = [f'{app_id}={store_id}' + (' (new)' if new else '')
             for app_id, store_id, new in summary['joins']]
    print('iOS joins:', ', '.join(joins) or 'none')
    icons = summary['icons']
    print(f"Icons: {summary['icons_fetched']} downloaded, {len(icons)} new/changed"
          + (f" ({', '.join(icons)})" if icons else ''))
    print('Missing from Google Play:', ', '.join(summary['missing']) or 'none')
    print(f"{CATALOGUE}: {summary['apps']} apps, {summary['old_size']} -> {summary['size']} "
          f"bytes (limit {MAX_BYTES}), {'changed' if summary['changed'] else 'unchanged'}")
    days = summary['last_commit_days']
    if days is None:
        print('Keepalive: last commit unknown, skipped')
    else:
        action = ('not needed' if not summary['keepalive']
                  else f'would write {KEEPALIVE}' if dry_run else f'wrote {KEEPALIVE}')
        print(f'Keepalive: last commit {days} days ago, {action}')
    if dry_run:
        print('Dry run: nothing written.')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Sync apps.json and icons/ with Google Play and the App Store.')
    parser.add_argument('--dry-run', action='store_true',
                        help='fetch, merge and validate, but write nothing')
    parser.add_argument('--root', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        help='catalogue checkout (default: this repository)')
    args = parser.parse_args(argv)
    try:
        summary = sync(args.root, dry_run=args.dry_run)
    except (SyncError, OSError, ValueError, http.client.HTTPException) as error:
        _annotate('error', f'sync aborted: {error}')
        return 1
    _print_summary(summary, args.dry_run)
    for app_id in summary['missing']:
        _annotate('warning', f'{app_id} is in {CATALOGUE} but not on Google Play; '
                             'entry kept, remove it by hand if it was unpublished')
    for bundle in summary['unmatched_ios']:
        _annotate('warning', f'App Store app {bundle} has no Google Play entry; not added')
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors='backslashreplace')
    sys.exit(main())
