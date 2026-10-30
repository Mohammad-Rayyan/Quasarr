## ADDED Requirements

### Requirement: Hints file is optional and failure-safe
Quasarr SHALL read `a4u_hints.json` from its config directory, reloading it when the file's mtime changes. A missing, unreadable or malformed file SHALL be treated as empty hints and SHALL NOT fail or slow any search.

#### Scenario: No hints file
- **WHEN** `a4u_hints.json` does not exist
- **THEN** a4, sh and tc searches behave as they did before this change

#### Scenario: Malformed hints file
- **WHEN** `a4u_hints.json` contains invalid JSON
- **THEN** searches proceed with no hints and no exception reaches the caller

### Requirement: Hint titles are tried first
For an IMDb search, a4, sh and tc SHALL try that IMDb's hint titles for the source (`titles[<initials>]`, then `titles["*"]`) before their existing title candidates.

#### Scenario: Site-only romaji
- **WHEN** hints give `titles.sh = ["Nijusseiki Denki Mokuroku Eureka Evrika"]` for the searched IMDb ID
- **THEN** sh queries the site with that title first

### Requirement: Hinted episode URLs answer the request directly
When a hint maps the requested episode (the absolute number for a4/sh, `SxxEyy` for tc) to a URL, the source SHALL fetch that URL on the currently configured host and emit releases from it without running a site search.

#### Scenario: Host rotated since the hint was written
- **WHEN** the hint URL is `https://old-host/episode/x/` and the configured host is `new-host`
- **THEN** the source fetches `https://new-host/episode/x/`

### Requirement: Unparseable release titles are relabelled to the Sonarr series title
For IMDb searches on a4, sh and tc, any release title that Sonarr `/parse` does not resolve to a series with the searched IMDb ID SHALL have its series-name segment replaced by the Sonarr series title (or the hint `title`), with the download link regenerated. Titles that Sonarr already resolves to the searched series SHALL be left unchanged.

#### Scenario: Unknown Series fixed
- **WHEN** sh emits `[SHAH] Nijusseiki Denki Mokuroku Eureka Evrika - 007 [1080p][WEB-DL][x264]` for IMDb `tt38939446`, and Sonarr parse returns no series
- **THEN** the emitted title is `[SHAH] Sparks of Tomorrow - 007 [1080p][WEB-DL][x264]`

#### Scenario: Scene-season title preserved
- **WHEN** Sonarr parses `[SHAH] Dr. Stone Science Future - 002 …` to the searched series
- **THEN** the title is emitted unchanged

#### Scenario: Sonarr unreachable
- **WHEN** the Sonarr parse call fails
- **THEN** titles are emitted unchanged

### Requirement: TopCinema season map
tc SHALL rewrite a parsed card's season from `siteSeason` to `season` when a hint season-map entry for `tc` matches the card. A match requires an equal season and, when `siteTitle` is set, an equal normalized card name. In search, entries come from the searched IMDb ID. In feed, entries from all hinted series are matched by `siteTitle` only.

#### Scenario: Link Click 2026 season
- **WHEN** Sonarr searches Link Click S04E08 and the site card is `Shiguang Dailiren` season 3 episode 8, with the map `{siteTitle: "Shiguang Dailiren", siteSeason: 3, season: 4}`
- **THEN** tc emits `Shiguang.Dailiren.S04E08.…-TCinema`

### Requirement: Site hostnames are published for the scout
Quasarr SHALL write the configured a4, sh, tc, ak, tk and wt hostnames to `a4u_sites.json` in its config directory whenever they differ from the file's contents, using an atomic replace.

#### Scenario: Hostname changed in the UI
- **WHEN** the a4 hostname is changed and the next search runs
- **THEN** `a4u_sites.json` contains the new a4 hostname
