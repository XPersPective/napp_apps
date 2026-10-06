"""Moves apps from pending.json to apps.json once their Google Play page is live.

Apps read only apps.json, so an entry never shows a broken store link. Run by
.github/workflows/promote-live.yml every day; safe to run by hand.
"""
import json
import urllib.error
import urllib.request

PENDING, CATALOGUE = 'pending.json', 'apps.json'


def live(package: str) -> bool:
    url = f'https://play.google.com/store/apps/details?id={package}&hl=en&gl=TR'
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def main() -> None:
    with open(PENDING, encoding='utf-8') as f:
        pending = json.load(f)
    with open(CATALOGUE, encoding='utf-8') as f:
        catalogue = json.load(f)
    known = {app['id'] for app in catalogue['apps']}
    waiting = []
    for app in pending['apps']:
        if app['id'] in known:
            continue
        if app.get('androidPackage') and live(app['androidPackage']):
            catalogue['apps'].append(app)
            print('promoted', app['id'])
        else:
            waiting.append(app)
    pending['apps'] = waiting
    for path, data in ((CATALOGUE, catalogue), (PENDING, pending)):
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
