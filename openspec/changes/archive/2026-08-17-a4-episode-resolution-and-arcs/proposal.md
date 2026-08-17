## Why

Sonarr interactive search for Bleach absolute episode 44 only returns an A4U `WEBDL-720p` release even though Anime4up's TYBW episode page publishes FHD 1080p. The a4 adapter reads episode numbers from unreliable URL slugs and treats split-arc site entries (`bleach-sennen-kessen-hen-…`) as unrelated series, so it scrapes the wrong page (old Bleach ep 330, 720p-only) instead of the arc page the user is looking at.

## What Changes

- Prefer episode numbers from listing card text (`.ep_num`) over slug regex; tighten the slug fallback so `الحلقة-44pss` no longer parses as 44.
- Use the card-derived number when building Sonarr release titles, not a second pass over the episode URL slug.
- Restrict episode link discovery to listing cards so sidebar/related `/episode/` hrefs cannot win.
- When multiple anime4up entries match the same franchise (main series + arc pages), map Sonarr absolute episode numbers onto the correct entry by walking candidates and matching local episode counts (and, where useful, XEM season names) so TYBW absolute 410 lands on `kashin-tan` ep 4 with 1080p.
- Translate Sonarr scene absolute numbers to canonical absolute numbers before arc mapping (Bleach scene 44 → canonical 410), using Sonarr's current-search metadata to disambiguate repeated scene numbers.
- Add regression tests covering slug lies, missing-number slugs, and a multi-arc absolute search fixture.

## Capabilities

### New Capabilities

- `a4-anime4up-search`: Episode discovery and release emission for the anime4up (`a4`) Quasarr search source — numbering, multi-entry arc resolution, and resolution reporting for Sonarr absolute searches.

### Modified Capabilities

- (none — OpenSpec was just initialized; no baseline specs yet)

## Impact

- Code: `quasarr/search/sources/a4.py` (primary), possibly light use of `quasarr/providers/xem_metadata.py` for season/arc titles; download twin `quasarr/downloads/sources/a4.py` unchanged unless resolution selection needs the corrected title.
- Tests: new unit tests under `tests/` with HTML fixtures (no live site dependency in CI).
- Runtime: slightly more page fetches when exploring arc candidates; bounded by existing `MAX_SEARCH_CANDIDATES` / `MAX_EPISODE_PAGES`.
- Ops: rebuild the `Dockerfile.a4u` overlay image after merge so the NAS Quasarr picks up the adapter.
