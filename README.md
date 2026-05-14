# Daily Digest

A personal news aggregator. Each morning at 07:00 CET, a scheduled Claude agent:

1. Reads `sources.yaml`
2. Crawls each source for items from the last ~36h
3. Caps per-source and per-topic counts so no single outlet dominates
4. Clusters stories into topics, deduplicates near-identical ones
5. Generates `index.html` and pushes to this repo
6. GitHub Pages serves the page within ~30 seconds

## Reading the digest

Visit the GitHub Pages URL for this repo (Settings → Pages → enable, source: `main` / root).

## Changing sources

Edit [`sources.yaml`](./sources.yaml). The next run picks up the changes — no redeploy needed.

## Quality bar

Sources are vetted for editorial rigor and methodology. Op-eds, press-release rewrites, and unchecked claims are filtered out. The aim is high signal, low volume.
