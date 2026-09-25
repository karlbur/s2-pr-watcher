# s2-pr-watcher

Dockerized GitHub self-hosted runners that automatically review pull requests with a local LLM. When a PR is opened, a runner picks up the job, sends the diff to an OpenAI-compatible endpoint (e.g. a LiteLLM proxy), and posts the model's review as a PR comment.

## How it works

- One Docker container per target repository, registered as a self-hosted GitHub Actions runner (`docker-compose-runners.yml`).
- `scripts/ai_review.py` fetches the PR diff file-by-file, skips generated/binary files, batches the rest, and reviews each batch via the LLM endpoint — so large PRs never blow up the context window.
- `examples/ai-review.yml` is the workflow to drop into each target repository.

## Quick start

```bash
cp .env.example .env   # fill in runner registration tokens and LLM settings
docker compose -f docker-compose-runners.yml up -d --build
```

See `.env.example` and `examples/ai-review.yml` for all configuration options.
