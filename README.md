# RSS Aggregator

A GitHub Actions-based RSS feed aggregator with AI-powered classification and scoring.

## Features

- **RSS Collection**: Fetches feeds from OPML-configured sources in parallel
- **Aliveness Monitoring**: HTTP health checks for all feed endpoints
- **AI Classification**: Categorizes articles and scores them 1-10 using OpenAI/Claude
- **GitHub Pages Dashboard**: Dark-themed web UI with filtering and search
- **Deduplication**: Skips already-processed articles across daily runs

## Setup

1. **Add your feeds**: Edit `feeds.opml` with your RSS sources
2. **Set AI API key**: Add `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` as a repository secret
3. **Configure**: Edit `config.yml` to change AI provider, categories, or scoring rules
4. **Enable GitHub Pages**: Settings → Pages → Source: GitHub Actions

## Usage

```bash
# Local run
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
python -m src.main
```

## Configuration

| File | Purpose |
|------|---------|
| `feeds.opml` | RSS feed sources (OPML format) |
| `config.yml` | AI provider, categories, scoring weights |
| `.github/workflows/collect.yml` | Cron schedule (default: daily 06:00 UTC) |

## Architecture

```
GitHub Actions (daily cron)
  → Parse OPML → Fetch RSS (parallel) → Dedup → Aliveness check
  → AI classify/score → Save JSON → Commit → GitHub Pages deploy
```
