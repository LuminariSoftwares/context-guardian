# Releasing

A release is published by GitHub Actions, not from a laptop. Publishing a GitHub Release runs
[`.github/workflows/release.yml`](../.github/workflows/release.yml), which checks the version, runs the tests, builds
both packages and uploads them with **trusted publishing**: PyPI and npm each trust this one workflow file and accept
a short-lived OIDC credential GitHub mints for the job. No API key is stored anywhere, and every file on PyPI and
every npm version carries a signed provenance attestation that names the commit it was built from.

> **Until the one-time setup below is done, publishing a GitHub Release makes this workflow fail without publishing
> anything.** The registries reject the OIDC credential because they have not been told to trust the workflow yet.
> Nothing is half-uploaded by that failure; do the setup, then re-run the failed jobs (Actions -> the failed run ->
> Re-run failed jobs).

## One-time setup (owner only)

Do all three. Each is free.

### 1. GitHub: create the `pypi` environment

1. Open <https://github.com/LuminariSoftwares/context-guardian/settings/environments>
   (repository -> **Settings** -> **Environments**).
2. Click **New environment**, name it exactly `pypi`, click **Configure environment**.
3. Optional but recommended: under **Deployment branches and tags** choose **Selected branches and tags** and add the
   tag rule `v*`, so only a release tag can use it. Optionally add yourself under **Required reviewers** if you want
   to click "approve" before each PyPI upload.
4. Click **Save protection rules**.

Success: the Environments page lists `pypi`.

### 2. PyPI: add a trusted publisher for `context-guardian`

1. Sign in at <https://pypi.org> and open <https://pypi.org/manage/project/context-guardian/settings/publishing/>
   (**Your projects** -> `context-guardian` -> **Manage** -> **Publishing**).
2. Under **Add a new publisher**, pick the **GitHub** tab and fill in:
   - Owner: `LuminariSoftwares`
   - Repository name: `context-guardian`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. Click **Add**.

Success: the page lists a GitHub publisher `LuminariSoftwares/context-guardian` with workflow `release.yml` and
environment `pypi`. After the first trusted release you can delete the old PyPI API token (**Account settings** ->
**API tokens**).

### 3. npm: add a trusted publisher for `dsh-context-guardian`

1. Sign in at <https://www.npmjs.com> and open <https://www.npmjs.com/package/dsh-context-guardian/access>
   (package `dsh-context-guardian` -> **Settings**).
2. Under **Trusted publishing**, choose **GitHub Actions** and fill in:
   - Organization or user: `LuminariSoftwares`
   - Repository: `context-guardian`
   - Workflow filename: `release.yml`
   - Environment name: leave empty
3. Tick **Allow npm dist-tag**. A prerelease is published under the `beta` tag and then moved to `latest` with
   `npm dist-tag add`; trusted publishing only allows that second command when this box is ticked. Without it the
   version is published but `latest` keeps pointing at the previous version, and the job fails at that step.
4. Click **Set up connection** (or **Save**).
5. Optional, after the first trusted release works: on the same page set **Publishing access** to
   **Require two-factor authentication and disallow tokens**, so nothing but this workflow can publish.

Success: the page lists the GitHub Actions publisher for `LuminariSoftwares/context-guardian`, workflow `release.yml`.

## Every release

The tag, `pyproject.toml` and `package.json` must all name the same version, or the `verify` job stops the release
before anything is built. npm and PyPI spell prereleases differently, so:

| Tag | `package.json` `version` | `pyproject.toml` `version` |
| --- | --- | --- |
| `v0.12.0` | `0.12.0` | `0.12.0` |
| `v0.12.0-beta.1` | `0.12.0-beta.1` | `0.12.0b1` |

Check before you tag (exit 0 and `versions agree` means the release will pass the version check):

```bash
python scripts/release_versions.py --tag v0.12.0
```

1. **Bump-version PR.** On a branch, set the version in `pyproject.toml` and `package.json` (table above), move the
   `## Unreleased` notes in `CHANGELOG.md` under a heading for the new version, open a PR, wait for green CI.
2. **Merge** the PR into `main`.
3. **Create the GitHub Release.** <https://github.com/LuminariSoftwares/context-guardian/releases/new>:
   - **Choose a tag** -> type `v0.12.0` (the new version) -> **Create new tag on publish**, target `main`.
   - Title: `v0.12.0`.
   - Notes: paste that version's section from `CHANGELOG.md`.
   - Tick **Set as a pre-release** for a `-beta.N` version.
   - Click **Publish release**.
4. **Watch the release workflow.** <https://github.com/LuminariSoftwares/context-guardian/actions/workflows/release.yml>.
   Jobs: `verify` (version check + tests) -> `pypi-build` -> `pypi` (upload), and `npm`. All four green means both
   packages are out. A red `verify` means nothing was published: fix the version, delete the release and its tag,
   and start again from step 1.
5. **Verify on the registries.**
   ```bash
   pip index versions context-guardian        # newest version listed first
   npm view dsh-context-guardian dist-tags    # latest (and beta for a prerelease) = the new version
   ```
   On <https://pypi.org/project/context-guardian/> each file shows a **Verified details** / provenance badge; on
   <https://www.npmjs.com/package/dsh-context-guardian> the version shows **Provenance** linking to the workflow run.

## Dry run

Builds and checks everything, publishes nothing:
<https://github.com/LuminariSoftwares/context-guardian/actions/workflows/release.yml> -> **Run workflow** -> leave
**dry_run** ticked -> **Run workflow**. A pull request that edits `release.yml` or `scripts/release_versions.py` runs
the same dry run. In a dry run the version check only warns, and its log says whether a release from that commit
would pass.

To retry a release without creating a new one (for example, after finishing the one-time setup): **Run workflow**,
untick **dry_run**, set **tag** to the release tag (e.g. `v0.12.0`). If you limited the `pypi` environment to `v*`
tags (setup step 1.3), start that run from the tag: **Use workflow from** -> **Tags** -> `v0.12.0`. Simpler: re-run
the failed jobs of the original run, which already runs on the tag.
