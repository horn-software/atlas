# Publishing Atlas to PyPI

## One-time setup

### 1. Create a PyPI account

Go to pypi.org → Register. Use the email `atlas@hornsa.com` if available, otherwise your personal email.

### 2. Create a .pypirc file

Create `~/.pypirc` (your home directory):

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-xxxxxxxxxxxxxxxxxxxxxxxxxxxx

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Replace the `password` values with API tokens from your PyPI account (Account Settings → API tokens → Add API token).

**IMPORTANT:** Never commit `~/.pypirc` — it contains your API token. It lives in your home directory, not the project.

### 3. Install build tools

```bash
pip install build twine
```

## Publishing a new version

### 1. Update version in pyproject.toml

Edit the `version` field in [pyproject.toml](../pyproject.toml):

```toml
[project]
version = "0.1.1"  # increment this
```

### 2. Build the distribution

From the project root (where `pyproject.toml` lives):

```bash
python -m build
```

This creates:
- `dist/atlas_mcp-0.1.1-py3-none-any.whl`
- `dist/atlas_mcp-0.1.1.tar.gz`

### 3. Test on TestPyPI first (recommended)

```bash
twine upload --repository testpypi dist/*
```

Install from TestPyPI to verify:
```bash
pip install --index-url https://test.pypi.org/simple/ atlas-mcp
```

### 4. Publish to PyPI

```bash
twine upload dist/*
```

### 5. Verify the listing

```
https://pypi.org/project/atlas-mcp/
```

Check that the README renders correctly, the version is right, and all classifiers appear.

## Updating the README on PyPI

PyPI pulls the `README.md` from `pyproject.toml`'s `readme` field. Edit `atlas/README.md`, bump the version, and republish.

## Version numbering

Use semantic versioning: `MAJOR.MINOR.PATCH`

- `PATCH` — bug fixes, no new features
- `MINOR` — new features, backwards compatible
- `MAJOR` — breaking changes

## Automating with GitHub Actions (future)

Create `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI
on:
  push:
    tags:
      - 'v*'
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install build twine
      - run: python -m build
      - run: twine upload dist/*
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}
```

Add `PYPI_TOKEN` as a secret in GitHub → horn-software/atlas → Settings → Secrets.
