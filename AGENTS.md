# Agent Working Rules

## Before Commits

- Run `/review` before creating any commit.
- Run the relevant checks for the files changed.
- For release, installer, or GitHub Pages changes, run:

```sh
python -m build
python scripts/build_release_assets.py
python scripts/check_release_readiness.py
```

## Release Invariants

- `pyproject.toml` is the package version source of truth.
- Git tags must match the package version as `vX.Y.Z`.
- GitHub Release titles must be the version tag only, for example `v0.2.0`.
- Release assets and `SHA256SUMS.txt` must only reference the current package version.
- GitHub Pages download links must use the `releases/latest/download/...` asset URLs.
