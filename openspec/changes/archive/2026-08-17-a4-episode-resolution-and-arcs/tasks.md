## 1. Episode number from listing cards

- [x] 1.1 Add `_episode_entries(page)` that returns `(number, url)` from `.anime-card-container` / `.ep_num`, with tightened slug fallback (`(?![0-9a-z])`)
- [x] 1.2 Rewrite `_find_episode_url` to iterate `_episode_entries` across paginated listing pages and return `(url, episode_number)` (now `_locate_episode`)
- [x] 1.3 Thread the resolved episode number into `_releases_from_episode_page` (stop re-parsing the episode URL slug for titles)
- [x] 1.4 Stop using bare `_episode_links` for matching (keep only for movie single-entry edge if still needed)

## 2. Split-arc absolute mapping

- [x] 2.1 Add franchise/arc grouping helper (`_franchise_chain`: TV-only, excludes movies/specials and `مدبلج` dub duplicates)
- [x] 2.2 When absolute `episode=N` misses on the first candidate, try other title-matched candidates for local `N` (subsumed by the cumulative walk over the ordered chain)
- [x] 2.3 If still missing, compute cumulative arc offsets from listing episode counts and map `N` → `(candidate, local)`; fetch that episode page
- [x] 2.4 Evaluate XeM soft-ranking: intentionally not used because year-ascending + count + search-order resolves Bleach 410 deterministically without a new live dependency
- [x] 2.5 Keep season-relative searches returning `[]` (existing absolute-only guard)
- [x] 2.6 Translate Sonarr scene absolute numbering to canonical absolute numbering using the currently searched episode's newest `lastSearchTime`

## 3. Tests and fixtures

- [x] 3.1 HTML fixtures added as inline builders in `tests/test_a4_source.py` (junk slug `44pss`→330, slug-without-digits →365, arc episode pages with 1080p/720p)
- [x] 3.2 Unit tests for `_episode_entries` / locate / title emission covering the `a4-anime4up-search` spec scenarios
- [x] 3.3 Unit test for arc offset mapping with a minimal multi-entry fixture (main + arc local counts, dub excluded)
- [x] 3.4 `ruff check` clean on `a4.py` + tests; new suite green (16/16); pre-existing unrelated `test_tc_source` failures noted

## 4. Deploy verification

- [x] 4.1 Rebuild `Dockerfile.a4u` overlay and redeploy Quasarr on the NAS (rollback image: `quasarr-a4u:rollback-20260817-0949`)
- [x] 4.2 Sonarr interactive search for episode ID 6260 (S17E44 / canonical 410) returns `[A4U] Bleach - 410 [1080p][WEB-DL][x264]` as `WEBDL-1080p`, `rejected=False`
