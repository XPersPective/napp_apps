# napp_apps

The app catalogue shown in the **Discover** tab of every CrazyPenguin app
(DoctorFilter and apps created from `napp_app_template`).

Apps read `apps.json` from:

```
https://raw.githubusercontent.com/XPersPective/napp_apps/HEAD/apps.json
```

They cache it for 24 hours, fall back to the last copy when offline, never list
themselves, and open Google Play on Android or the App Store on iOS.

## Adding an app

Apps live on Google Play under our developer account are added automatically
(see *Automatic sync*). Add others by hand, and only once they are **live** in a
store — a broken store link is worse than no entry.

1. Put a square PNG icon (256×256 is plenty) in `icons/<id>.png`.
2. Add an entry to `apps.json`:

```json
{
  "id": "my_app",
  "androidPackage": "com.crazypenguin.myapp",
  "appStoreId": "1234567890",
  "icon": "https://raw.githubusercontent.com/XPersPective/napp_apps/HEAD/icons/my_app.png",
  "name": { "en": "My App", "tr": "Uygulamam" },
  "description": { "en": "One short line.", "tr": "Kısa bir cümle." },
  "order": 2
}
```

- `androidPackage` and/or `appStoreId` — at least one; the store it lacks is
  simply not offered on that platform.
- `name` / `description`: language code → text. Missing languages fall back to
  English. Keep descriptions to one line and free of health or earnings claims.
- `icon` must be `https://`.
- `order`: lower comes first.

No release of any app is needed — the change reaches every app within a day.

## Automatic sync

`.github/workflows/sync-stores.yml` runs `tool/sync_stores.py` daily (or by
hand: Actions → Sync store catalogue → Run workflow). It reads only public
pages — the Google Play developer page (en-US, tr-TR) and the iTunes lookup for
our App Store account — and then:

- adds apps new on Google Play (English and Turkish name and short
  description, icon in `icons/`, next `order`);
- sets `appStoreId` where the App Store lists the same bundle id;
- refreshes icons whose store version changed.

Existing translations, `order` and icon URLs are never overwritten. Every entry
is validated before writing; any store error leaves `apps.json` untouched.
Preview with `python3 tool/sync_stores.py --dry-run`.

**Removals stay manual:** an app that disappears from Google Play is only listed
in `sync-report.json` and as a workflow warning — delete its entry by hand.

Schema: `ORTAK_UYGULAMA_STANDARDI.md` §3.6 (schema 1).
