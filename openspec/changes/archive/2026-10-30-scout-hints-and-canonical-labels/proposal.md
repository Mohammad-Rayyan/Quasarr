## Why

Missing Arabic hard-sub episodes often exist on the sites but Quasarr cannot surface them in a form Sonarr accepts:

- Site romaji differs from every Sonarr title (`Nijusseiki Denki Mokuroku` vs Sonarr's `Nijuuseiki…`), so Sonarr rejects the release as **Unknown Series** even though the episode is right (Sparks of Tomorrow E6–13 on SH).
- Site season numbering disagrees with TVDB (TopCinema `Shiguang.Dailiren.S03` is TVDB Link Click S04; `Yingdu.Pian.S02` is TVDB S03), so searches filter the right cards out and imports land on wrong episodes.
- SH only queries the IMDb-localized title — no Sonarr alternate-title fallback like a4 has.
- Some series are only findable by a human (or an agent) browsing the site; there is no way to teach Quasarr what was found.

A Claude Code "scout" job in claude-cron will browse the sites offline for hard-to-find episodes. This change gives Quasarr the read side: a hints file the scout writes, consumed by a4/sh/tc at search time, plus deterministic relabelling so a found release always parses in Sonarr.

## What Changes

- New helper `quasarr/search/sources/helpers/a4u_hints.py`: loads `<config>/a4u_hints.json` (mtime-cached), exposes per-IMDb site titles, episode URL overrides and season maps; publishes the current a4/sh/tc/ak/tk/wt hostnames to `<config>/a4u_sites.json` for the scout.
- **Canonical relabel**: for IMDb searches on a4/sh/tc, a release title Sonarr's `/parse` cannot resolve to the searched series is rewritten to Sonarr's own series title (and its download link regenerated). Titles Sonarr already resolves are left untouched — the site title can carry scene-season meaning (Dr. Stone *Science Future*).
- a4, sh, tc: hint titles are tried before the existing title candidates; hinted episode URLs answer the request directly.
- sh: falls back to Sonarr title/alternate titles (bounded) when the localized title finds nothing.
- tc: season map rewrites site season → TVDB season on parsed cards in search and feed.
- Dockerfile.a4u copies the new helper and the previously uncommitted ak/tk/wt adapters (now tracked in the repo).

## Capabilities

### New Capabilities
- `arabic-ddl-hints`: hints file contract, canonical relabel, and per-source consumption.

### Modified Capabilities
- None (a4 absolute/arc behaviour unchanged).

## Impact

- Code: `quasarr/search/sources/{a4,sh,tc}.py`, new helper, `Dockerfile.a4u`, tests.
- Runtime: one extra Sonarr `/parse` call per distinct release series title per search (in-process cached).
- Non-goals: no LLM calls inside Quasarr or on Sonarr's search path; no changes to download twins; ak/tk/wt only gain repo tracking, not hint support; no franchise tables in code.
