## Context

Quasarr answers Sonarr Newznab searches by scraping a4 (anime4up), sh (shahiid) and tc (TopCinema). Sonarr searches by IMDb ID; each source turns it into site titles. Failures we see in production are naming/numbering mismatches, not absence. An offline Claude Code scout (claude-cron) will discover the correct site title / episode URL / season mapping and record it; Quasarr must consume that without any LLM latency.

## Goals / Non-Goals

**Goals:** a stable file contract between scout and Quasarr; releases that Sonarr can always map; zero behaviour change when no hints exist and titles already parse.

**Non-Goals:** calling Claude from Quasarr; hint support for ak/tk/wt; editing download twins.

## Decisions

### Hints file `<config_dir>/a4u_hints.json`

`config_dir` is the directory of `shared_state.values["configfile"]` (`/config` in Docker, bind-mounted from `/app_data/quasarr`). Reloaded when mtime changes; any read/parse error yields empty hints (never fails a search).

```json
{
  "version": 1,
  "series": {
    "tt14976292": {
      "title": "Link Click",
      "titles":   {"tc": ["Shiguang Dailiren"], "*": ["Link Click"]},
      "episodes": {"a4": {"192": "/episode/jojo-...-2/"}, "tc": {"S04E08": "/episode/..."}},
      "seasons":  {"tc": [{"siteTitle": "Shiguang Dailiren Yingdu Pian", "siteSeason": 2, "season": 3},
                          {"siteTitle": "Shiguang Dailiren", "siteSeason": 3, "season": 4}]}
    }
  }
}
```

- Episode keys: absolute number as string for absolute sites (a4, sh); `SxxEyy` for tc.
- Episode URLs may be absolute or path-only. The host is always replaced with the currently configured hostname, because these sites rotate domains.
- `seasons[].siteTitle` is optional. When it is present it must match the card name (normalized: casefolded, alphanumerics only). A more specific (longer) siteTitle wins over a shorter one.

### Sites file `<config_dir>/a4u_sites.json`

`{"a4": "host", "sh": "host", ...}` is written atomically when a configured hostname differs from the last written value. The scout reads it to know where to browse. Hostnames in Quasarr.ini are encrypted and the scout cannot decrypt them.

### Canonical relabel only for unparseable titles

After a4/sh/tc produce IMDb-search releases, each distinct title is sent to Sonarr `/parse`. If the parsed `series.imdbId` equals the searched ID → keep. Otherwise the series-name segment is replaced with Sonarr's series title (hint `title` if set) and the link regenerated via `generate_download_link` with the same source URL/size/imdb. Name segment by shape:
- bracketed absolute: `[TAG] <name> - NNN [..]`
- scene: `<Name.Dotted>.SxxEyy.<rest>`

Rationale: relabelling everything would break scene-numbered series where the site title selects the season mapping (verified: `[SHAH] Dr. Stone Science Future - 002` maps to S4E2, `Dr. STONE - 002` would map to S1E2). `/parse` results are cached per title for 6 h; Sonarr unreachable → keep titles.

### Source consumption

- a4: `queries = hint titles + _title_candidates(...)`. If an episode hint exists for the canonical absolute number, fetch that page and emit via `_releases_from_episode_page` (label = first hint/series title) before any search.
- sh: same episode-hint shortcut via `_releases_from_episode`; queries = hint titles + localized + Sonarr titles, capped at `MAX_TITLE_QUERIES = 3`, stop at the first query with releases.
- tc: queries = hint titles + localized (first with releases). Season map is applied to parsed cards (`card["site_season"]` keeps the original value for season-pack URL lookup) in both search and feed. In feed, hints are matched by siteTitle only, because there is no IMDb ID. A `SxxEyy` episode hint builds a synthetic episode card.

## Risks / Trade-offs

- Wrong hint → wrong episode grabbed. Mitigation: the scout validates URLs before writing them, and Sonarr still checks the parsed episode against the request.
- Extra `/parse` calls are local and cheap. They are bounded by the number of distinct titles, which is at most about 6 per search.
