## Context

The anime4up (`a4`) Quasarr overlay scrapes Anime4up for Arabic-subtitled anime and emits absolute-numbered Sonarr releases like `[A4U] Bleach - 044 [720p][WEB-DL][x264]`.

Live reproduction (2026-08-17):

- Sonarr searched Bleach absolute ~44 via Quasarr and got only that 720p title.
- Site page for TYBW *Kashin-tan* ep 4 has FHD 1080p on mega/workupload/mediafire/gofile.
- Adapter instead resolved `/anime/bleach/` + slug `bleach-الحلقة-44pss`, whose card text and `h1` are episode **330**, with only HD 720p hosts.

Root causes:

1. Episode number is taken from the URL with `الحلقة[-\s]+(\d+)`, which matches junk suffixes (`44pss`) and misses most newer slugs that omit the number entirely (`الحلقة-smfpi`).
2. The site splits long franchises into separate `/anime/<arc>/` entries numbered from 1, while Sonarr uses one series with absolute (or season) numbering. Search currently returns the first title match that has *some* episode and never remaps absolute numbers across arcs.
3. With `useSceneNumbering` enabled, Sonarr sends `sceneAbsoluteEpisodeNumber` in the Newznab `q` parameter. Bleach S17E44 arrives as `44`, although the canonical absolute episode required for Anime4up arc mapping is `410`.

Constraints from AGENTS.md: keep the change inside `quasarr/search/sources/a4.py` (+ tests); no broad abstractions; work on `dev`; prefer bounded fetches already gated by `MAX_SEARCH_CANDIDATES` / `MAX_EPISODE_PAGES`.

## Goals / Non-Goals

**Goals:**

- Correct episode identity from listing pages so slug lies cannot produce the wrong release.
- Emit the resolution set actually present on the matched episode page (including 1080p when the site has it).
- Map Sonarr absolute episode numbers onto split-arc Anime4up entries for franchises like Bleach TYBW so absolute 410 → `kashin-tan` local ep 4.
- Regression coverage with offline HTML fixtures.

**Non-Goals:**

- Changing download-side hoster filtering (`downloads/sources/a4.py`) beyond what corrected titles already drive.
- Supporting season-relative Sonarr searches against absolute-only site entries (a4 already skips `season` truthy searches; keep that).
- Building a general multi-source episode mapper or rewriting XEM integration globally.
- Fixing unrelated Anime4up HTML churn outside episode listing / download-table contracts already documented in `a4.py`.

## Decisions

### 1. Card text owns episode numbers; slug is fallback only

**Choice:** Parse `(number, url)` from `.anime-card-container` → `.ep_num` text (`الحلقة N`). Fall back to a tightened slug pattern that requires a non-alnum boundary after the digits (`(?![0-9a-z])`) so `44pss` does not match.

**Why:** Live listing cards always show the real number; slugs are SEO junk. Restricting discovery to cards also drops sidebar/related episode links that `_episode_links` currently scoops up.

**Alternatives:** Fetch every episode page and read `h1` — accurate but expensive. Slug-only with a denylist of known junk patterns — fragile.

### 2. Carry the resolved number into release titles

**Choice:** `_find_episode_url` returns `(url, episode_number)` (or a small named tuple). `_releases_from_episode_page` uses that number instead of re-running `_episode_number(url)`.

**Why:** Even after finding the right URL, re-deriving from the slug reintroduces the same bug for title emission.

### 3. Arc mapping via candidate walk + local episode presence

**Choice:** When absolute `episode=N` is requested and the first matching anime page does not contain local episode `N`:

1. Keep all title-matched search candidates (already capped by `MAX_SEARCH_CANDIDATES`).
2. For each TV candidate, build the set of local episode numbers from listing cards (paginate until found or pages exhausted).
3. Prefer a candidate where local episode `N` exists (current behavior, but with correct numbers).
4. If none contain `N`, treat candidates that share a franchise prefix (normalized title token overlap / common slug stem like `bleach-sennen-kessen-hen-*`) as an ordered arc group (newest/year ascending using `_anime_year` + search-card order), sum prior arc lengths, and map absolute `N` → `(arc_entry, local = N - offset)` when `1 <= local <= arc_length`.

Optionally boost candidate ordering with TheXEM season names (`get_season_name` / `get_all_season_names`) when the IMDb/title path already has a library title — use as ranking hint only, not as hard gate, so missing XEM data does not break search.

**Why:** Matches how Anime4up actually publishes TYBW without requiring a hardcoded Bleach table. Absolute search remains the only path a4 answers (season-relative still skipped).

**Alternatives considered:**

- Hardcoded Bleach arc offsets — fast, unmaintainable.
- Always prefer longest / main-series entry — wrong for TYBW-only requests and still needs offsets for absolute numbers past 366.
- Full XEM scene→tvdb map — richer but heavier and not currently wired into a4; season *names* are enough as a soft signal.

### 4. Tests use HTML fixtures, not live HTTP

**Choice:** Unit-test helpers (`_episode_entries`, `_find_episode_url`, arc offset mapping, `_available_resolutions`) against checked-in HTML snippets captured from the live failure cases (`44pss` → 330, `smfpi` → 365, kashin-tan ep 4 download table).

**Why:** CI has no Anime4up access; fixtures pin the contract already documented in the module header.

### 5. Translate Sonarr scene numbering before arc mapping

**Choice:** For IMDb anime searches, inspect the configured Sonarr library record. When `useSceneNumbering` is enabled, fetch that series' episodes and translate the requested scene absolute number to `absoluteEpisodeNumber`. Scene numbers can repeat across seasons (Bleach has both old absolute 44 and S17E44 at scene absolute 44), so choose the match with the newest `lastSearchTime`; Sonarr updates that field for the episode whose interactive/automatic search triggered the Newznab request.

**Why:** The Newznab request does not include enough season context for anime scene searches. Sonarr's own current-search metadata is the authoritative disambiguator.

**Alternative:** Treat the incoming `q` as canonical absolute. This produced the original wrong result: scene 44 mapped to old Bleach episode 44 instead of canonical 410.

## Risks / Trade-offs

- [Wrong arc offset if site republishes incomplete arcs] → Prefer exact local-number hit before offset math; log which candidate/local ep was chosen.
- [Franchise grouping false positives] → Require shared normalized stem / high token overlap; do not merge unrelated “Bleach” movies into the TV arc chain (`want_movie` / `_anime_type` already separates movies).
- [Extra fetches for multi-candidate absolute search] → Stay within existing caps; stop when the mapped episode page is found.
- [XEM soft ranking unavailable] → Fall back to year + search relevance order only.

## Migration Plan

1. Land adapter + tests on `dev`.
2. Rebuild/redeploy the `Dockerfile.a4u` overlay on the NAS.
3. In Sonarr: interactive search Bleach absolute 410 (or TYBW ep 4) and confirm `[A4U] … - 410 … [1080p]…` appears; confirm absolute 44 no longer returns the `44pss`/330 page as 720p-only.
4. Rollback: revert overlay COPY of `a4.py` to previous image tag.

## Open Questions

- None blocking implementation. If TYBW absolute numbering in the user’s Sonarr diverges from “main 366 + prior TYBW arcs + local”, adjust offset order using live card episode counts rather than hardcoding.
