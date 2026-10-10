# Release checklist (owner actions)

These steps are external — they need accounts, credentials, or a decision that
only the owner can make. Everything the repository can do on its own is already
green; this is the human side of the publish gate.

## U1 — Rotate the OpenRouter key

The key in `tamias-agent/.env` is used only for the live act. Rotate it before
any public push so a leaked key can never be live.

1. Create a new key at the provider console and set a spend cap.
2. Replace `OPENROUTER_API_KEY` in `tamias-agent/.env` (never commit this file;
   it is `.gitignore`d).
3. Confirm the old key is revoked.

## U2 — PyPI account and explicit publish approval

`.github/workflows/publish.yml` publishes with OIDC Trusted Publishing; it needs
a matching pending publisher before it can run.

1. On PyPI, add a *pending publisher* for project `tamias`:
   - Owner: your GitHub user/org, Repository: `tamias`,
     Workflow: `publish.yml`, Environment: `pypi`.
2. Create the GitHub `pypi` environment (optionally protected).
3. **Approve the release explicitly.** Nothing is pushed anywhere without a
   tag and this approval.

To publish a release:

```sh
# from the repo, on the commit you intend to ship
git tag v0.1.0a4
git push origin master
git push origin v0.1.0a4          # only after explicit approval
```

The workflow refuses to publish when the tag and `pyproject.toml` version
disagree.

## U3 — GitHub repository settings

1. Description: "Local OpenAI/Anthropic-compatible proxy that meters traffic and
   records shadow routing decisions."
2. Topics: `llm`, `openai`, `anthropic`, `proxy`, `observability`, `cost`.
3. Security → enable private vulnerability reporting (README points at
   `SECURITY.md`).
4. Confirm the `audit` job's SBOM and license artifacts are enabled.

## U4 — Set the live-demo date

The live agent act is shown only after **two consecutive passing rehearsals**
(free models return 429). Pick the date after those two passes; if it fails
twice, run the offline acts and say so.

## Publish gate (all must be true)

- [ ] C1–C6 correctness green (verified).
- [ ] G1–G11 green (verified; CI carries build, smoke, audit).
- [ ] CX5 / RR review — independent reviewer must confirm.
- [ ] U1, U2, U3 done.
- [ ] Tag pushed and release approved by the owner.
