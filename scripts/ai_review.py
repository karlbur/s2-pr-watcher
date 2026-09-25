#!/usr/bin/env python3
"""s2-pr-watcher: AI code review for GitHub PRs.

Fetches the PR diff file-by-file via PyGithub, reviews it with an LLM through
a LiteLLM proxy (OpenAI-compatible API), and posts/updates a single summary
comment on the PR. Large diffs are handled by batching files and truncating
oversized patches, so one LLM call never sees an unbounded payload.

Required environment variables (normally provided by the GitHub Actions
workflow in the target repository):
  GITHUB_TOKEN      - token with pull-requests:write on the target repo
  GITHUB_REPOSITORY - "owner/repo" (set automatically by Actions)
  PR_NUMBER         - pull request number
  AI_REVIEW_MODEL   - model/deployment name exposed by LiteLLM

Optional:
  LITELLM_URL (default http://host.docker.internal:4000/v1)
  LITELLM_API_KEY, MAX_FILE_PATCH_CHARS, MAX_BATCH_CHARS
"""

import os
import re
import sys
import time

from github import Auth, Github
from openai import OpenAI

COMMENT_MARKER = "<!-- s2-pr-watcher -->"

# Lockfiles, vendored code, build artifacts and non-reviewable assets.
SKIP_PATTERNS = [
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|composer\.lock)$",
    r"(^|/)(vendor|node_modules|dist|build|target)/",
    r"\.min\.[a-z]+$",
    r"\.(png|jpe?g|gif|ico|svg|woff2?|ttf|eot|mp4|pdf|zip|tar|gz|bin|exe|so|dylib|dll)$",
]
SKIP_RE = re.compile("|".join(SKIP_PATTERNS), re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are a senior software engineer reviewing a GitHub pull request. "
    "For each file diff given, report ONLY real, actionable findings: bugs, "
    "security issues, logic errors, and significant performance problems. "
    "Do not comment on style, formatting, or trivialities. "
    "Answer in Markdown using this exact structure per file:\n"
    "### `path/to/file`\n"
    "- **[severity: high|medium|low]** finding (reference the code line/hunk)\n"
    "If a file has no notable issues, write exactly: `### \\`path/to/file\\`\nLGTM`. "
    "Be concise; never invent code that is not in the diff."
)


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(2)
    return value


def should_skip(filename):
    return bool(SKIP_RE.search(filename))


def truncate_patch(patch, limit):
    if len(patch) <= limit:
        return patch, False
    cut = patch[:limit].rfind("\n")
    cut = cut if cut > 0 else limit
    return f"{patch[:cut]}\n... (patch truncated: {len(patch)} chars total, showing {cut})", True


def batch_files(files, budget):
    """Greedily group (name, patch, truncated) items so each batch stays under `budget` chars."""
    batch, size = [], 0
    for name, patch, _truncated in files:
        cost = len(patch) + len(name) + 64
        if batch and size + cost > budget:
            yield batch
            batch, size = [], 0
        batch.append((name, patch, _truncated))
        size += cost
    if batch:
        yield batch


def review_batch(client, model, batch, max_retries=3):
    """One LLM call for a batch of files. Returns markdown text or raises."""
    parts = [
        f"FILE: {name}\n```diff\n{patch}\n```" for name, patch in batch
    ]
    user_msg = (
        "Review the following file diffs from pull request. "
        f"{len(batch)} file(s):\n\n" + "\n\n".join(parts)
    )
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()
        except Exception as err:  # openai APIError, timeouts, rate limits
            last_err = err
            wait = 2 ** attempt
            print(f"WARN: LLM call failed (attempt {attempt}/{max_retries}): {err} — retrying in {wait}s")
            time.sleep(wait)
    raise last_err


def upsert_comment(pr, bot_login, body):
    """Edit the bot's existing review comment instead of spamming new ones."""
    for comment in pr.as_issue().get_comments():
        if comment.user.login == bot_login and COMMENT_MARKER in comment.body:
            comment.edit(body)
            return "updated"
    pr.create_issue_comment(body)
    return "created"


def main():
    gh_token = env("GITHUB_TOKEN", required=True)
    repo_name = env("GITHUB_REPOSITORY", required=True)
    pr_number = env("PR_NUMBER", required=True)
    model = env("AI_REVIEW_MODEL", required=True)
    litellm_url = env("LITELLM_URL", "http://host.docker.internal:4000/v1")
    litellm_key = env("LITELLM_API_KEY", "sk-none")
    max_patch = int(env("MAX_FILE_PATCH_CHARS", "8000"))
    max_batch = int(env("MAX_BATCH_CHARS", "24000"))

    gh = Github(auth=Auth.Token(gh_token))
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(int(pr_number))
    bot_login = gh.get_user().login

    # Collect reviewable files; API omits `patch` for binaries and huge files.
    reviewable, skipped, no_patch = [], [], []
    for f in pr.get_files():
        if should_skip(f.filename):
            skipped.append(f.filename)
        elif not f.patch:
            no_patch.append(f.filename)
        else:
            patch, was_truncated = truncate_patch(f.patch, max_patch)
            reviewable.append((f.filename, patch, was_truncated))

    print(f"PR #{pr_number} in {repo_name}: {len(reviewable)} reviewable, "
          f"{len(skipped)} skipped, {len(no_patch)} without patch")

    if not reviewable:
        body = (
            f"{COMMENT_MARKER}\n## 🤖 AI Code Review\n\n"
            "No reviewable text changes found in this PR "
            f"({len(skipped)} file(s) skipped as generated/vendored, "
            f"{len(no_patch)} binary/large file(s))."
        )
        upsert_comment(pr, bot_login, body)
        return

    client = OpenAI(base_url=litellm_url, api_key=litellm_key)
    batches = list(batch_files(reviewable, max_batch))
    sections, failures = [], 0
    for batch in batches:
        try:
            sections.append(review_batch(client, model, batch))
        except Exception as err:
            failures += 1
            names = ", ".join(n for n, _, _ in batch)
            print(f"ERROR: review failed for batch [{names}]: {err}", file=sys.stderr)
            sections.append(
                "### ⚠️ Review unavailable\n"
                f"The model could not review: `{names}`.\n"
                f"```\n{err}\n```"
            )

    truncated_note = (
        "\n\n> ℹ️ One or more large patches were truncated to fit the model context."
        if any(t for _, _, t in reviewable) else ""
    )
    status = "✅" if failures == 0 else f"⚠️ ({failures} batch(es) failed)"
    body = (
        f"{COMMENT_MARKER}\n## 🤖 AI Code Review — {status}\n"
        f"PR #{pr_number} · model `{model}` · {len(reviewable)} file(s) reviewed"
        f"{truncated_note}\n\n" + "\n\n---\n\n".join(sections)
    )
    action = upsert_comment(pr, bot_login, body)
    print(f"Review comment {action}: {len(sections)} section(s), {failures} failure(s)")

    if failures == len(batches):
        sys.exit(1)  # every batch failed


if __name__ == "__main__":
    main()
