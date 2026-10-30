## 1. Repo hygiene

- [x] 1.1 Track ak/tk/wt search + download adapters pulled from the running image; add them to Dockerfile.a4u COPY lines and registration assert

## 2. Hints helper

- [x] 2.1 `helpers/a4u_hints.py`: mtime-cached loader, `hint_titles`, `hint_episode_url`, `tc_season_map`, `publish_site`
- [x] 2.2 `relabel_releases(shared_state, releases, imdb_id, initials)` with Sonarr `/parse` check, 6 h cache, bracketed + scene name shapes
- [x] 2.3 Unit tests: missing/malformed file, host rewrite, relabel unknown vs known vs Sonarr failure, season-map specificity

## 3. Sources

- [x] 3.1 a4: hint titles first, episode-hint shortcut, relabel IMDb results, publish site
- [x] 3.2 sh: hint titles + localized + Sonarr titles (cap 3), episode-hint shortcut, relabel, publish site
- [x] 3.3 tc: hint titles, season map in search + feed (keep `site_season` for pack URL), `SxxEyy` episode hint, relabel, publish site
- [x] 3.4 Fixture-backed tests for sh fallback/hint and tc season map; existing suites stay green

## 4. Deploy

- [x] 4.1 Add helper to Dockerfile.a4u, rebuild `quasarr-a4u:local` on NAS, recreate container with same env/mount/port
- [x] 4.2 Verify: `a4u_sites.json` written; Sparks of Tomorrow E7 interactive search returns an approved release
