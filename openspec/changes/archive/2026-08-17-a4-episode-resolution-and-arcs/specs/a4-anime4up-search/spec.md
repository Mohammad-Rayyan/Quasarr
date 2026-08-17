## ADDED Requirements

### Requirement: Episode numbers come from listing cards
The a4 search source SHALL resolve episode numbers from Anime4up listing card text (`.ep_num` within `.anime-card-container`) before falling back to URL slug parsing. Slug parsing MUST NOT treat alphanumeric suffixes after digits as part of the episode number (e.g. `الحلقة-44pss` MUST NOT resolve as episode 44).

#### Scenario: Junk slug with correct card text
- **WHEN** a listing card shows `الحلقة 330` and the episode URL slug contains `الحلقة-44pss`
- **THEN** the resolved episode number is 330

#### Scenario: Slug without digits
- **WHEN** a listing card shows `الحلقة 365` and the episode URL slug contains `الحلقة-smfpi` with no digits
- **THEN** the resolved episode number is 365

#### Scenario: Sidebar episode links ignored
- **WHEN** the anime page HTML contains `/episode/` links outside listing cards
- **THEN** those links are not used as candidates for episode matching

### Requirement: Release titles use the resolved episode number
The a4 search source SHALL emit Sonarr release titles using the episode number resolved from the listing (or arc mapping), not a second parse of the episode page URL slug.

#### Scenario: Title matches resolved number
- **WHEN** absolute search finds listing episode 330 whose slug falsely looks like 44
- **THEN** the emitted title uses `330` (zero-padded to three digits) and does not use `044`

### Requirement: Absolute search maps across split-arc entries
When Sonarr requests an absolute episode number and multiple Anime4up TV entries match the franchise, the a4 search source SHALL locate the entry whose local episode list contains that absolute number when possible. If no entry contains the absolute number locally, it SHALL map the absolute number onto an ordered chain of related arc entries using cumulative local episode counts and fetch that local episode page.

#### Scenario: Exact local hit preferred
- **WHEN** absolute episode `N` is requested and a title-matched TV entry lists local episode `N`
- **THEN** that entry’s episode page is used and no arc-offset remapping is applied

#### Scenario: TYBW-style absolute past main series
- **WHEN** absolute episode `410` is requested for Bleach and Anime4up exposes a main series (~366 eps) plus TYBW arc entries numbered from 1
- **THEN** search returns releases for the mapped arc-local episode (e.g. `kashin-tan` local 4), not a junk slug from the main series listing

#### Scenario: Season-relative search still skipped
- **WHEN** Sonarr sends a season-relative search (`season` truthy) for a TV show
- **THEN** a4 returns no results (absolute-only behavior unchanged)

### Requirement: Sonarr scene absolute numbers map to canonical absolute numbers
When Sonarr has `useSceneNumbering` enabled and sends a scene absolute episode number, the a4 search source SHALL resolve the episode Sonarr is currently searching and use its canonical `absoluteEpisodeNumber` for Anime4up arc mapping and release-title emission. If multiple Sonarr episodes share the scene absolute number, the source SHALL prefer the episode with the newest `lastSearchTime`.

#### Scenario: Bleach scene 44 maps to canonical 410
- **WHEN** Sonarr searches Bleach S17E44 using scene absolute `44`, and that episode has canonical absolute `410`
- **THEN** a4 maps Anime4up using absolute `410` and emits release titles containing `- 410`

#### Scenario: Ambiguous scene absolute number
- **WHEN** an older episode and the currently searched episode both have scene absolute `44`
- **THEN** a4 selects the episode with the newest Sonarr `lastSearchTime`

### Requirement: Reported resolutions match the episode download table
For the matched episode page, the a4 search source SHALL emit one release per distinct resolution present in `.episode-download-container .td-quality` cells.

#### Scenario: 1080p present on page
- **WHEN** the matched episode download table includes `FHD 1080p` and `HD 720p`
- **THEN** search results include both a `[1080p]` and a `[720p]` A4U release for that episode
