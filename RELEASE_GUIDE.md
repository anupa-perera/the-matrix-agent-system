# Release Guide

Use this when you want to publish a GitHub Release that normal users can download.

## What The Release Contains

The release workflow builds and uploads:

- `the-matrix-agent-system-<version>-py3-none-any.whl`
- `the_matrix_agent_system-<version>.tar.gz`
- `the-matrix-agent-system-windows.zip`
- `the-matrix-agent-system-mac-linux.tar.gz`
- `SHA256SUMS.txt`
- generated release notes

The Windows ZIP is the main download for non-technical Windows users. It includes:

- `START_HERE_WINDOWS.txt`
- `Install The Matrix.cmd`
- `Start The Matrix.cmd`
- the package source needed for a local install

The macOS/Linux archive includes:

- `START_HERE_MAC_LINUX.txt`
- `install.sh`
- `start.sh`
- the package source needed for a local install

## Before Publishing

Run these locally:

```powershell
cd F:\sideProjects\the-matrix-agent-system
git status
uv run --system-certs python -m pytest
uvx --system-certs ruff check .
uv run --system-certs python -m build
uv run --system-certs python scripts/build_release_assets.py
```

Check the generated files:

```powershell
Get-ChildItem dist
Get-ChildItem release-assets
```

Open the generated release notes:

```powershell
notepad release-assets\RELEASE_NOTES.md
```

## Publish

1. Commit all release-prep changes.
2. Push `main`.
3. Create and push a version tag.

```powershell
git add .
git commit -m "Prepare GitHub release publishing"
git push origin main
git tag v0.2.2
git push origin v0.2.2
```

GitHub Actions will run the `Release` workflow and create the GitHub Release automatically.

The `Pages` workflow publishes the friendly download page from `site/`.

If Pages has not been enabled yet, go to:

```text
GitHub repo -> Settings -> Pages -> Build and deployment -> Source
```

Select:

```text
GitHub Actions
```

After the workflow finishes, the download page should be available at:

```text
https://anupa-perera.github.io/the-matrix-agent-system/
```

## Manual Checks After GitHub Publishes

Open:

```text
https://github.com/anupa-perera/the-matrix-agent-system/releases
```

Confirm the release has these assets:

- Windows ZIP
- macOS/Linux archive
- wheel
- source package
- `SHA256SUMS.txt`

Then test the Windows user path:

1. Download the Windows ZIP from the release.
2. Right-click it and choose **Extract All**.
3. Open the extracted folder.
4. Double-click `Install The Matrix.cmd`.
5. Confirm the browser onboarding opens.

## If The Release Workflow Fails

Open the failed GitHub Actions run and check the failed step.

Common causes:

- tests failed
- the tag already has a release
- GitHub Actions does not have permission to write releases

If permissions fail, go to:

```text
GitHub repo -> Settings -> Actions -> General -> Workflow permissions
```

Select:

```text
Read and write permissions
```

Then rerun the failed workflow.
