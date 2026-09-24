# Releasing

A release is a version tag. Pushing `v<version>` runs the Release workflow
(`.github/workflows/release.yml`): it builds the wheel and source archive,
checks that their version matches the tag, runs the demo from the built wheel,
publishes both files to PyPI, and creates a GitHub release with them.

## Once: let GitHub publish to PyPI

Publishing uses PyPI trusted publishing, so no PyPI token is stored anywhere.

1. On PyPI, under Your account, Publishing, add a pending publisher (it becomes
   a normal one after the first release):
   - PyPI project name: `chatlore`
   - Owner: `cl0ver012`
   - Repository name: `chatlore`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. On GitHub, under Settings, Environments, create an environment named `pypi`.
   Adding yourself as a required reviewer makes every release wait for your
   approval before anything is published.

## Every release

1. On a branch, set the version in `pyproject.toml` and
   `src/chatlore/__init__.py`, then run `uv lock` so the lockfile records it.
2. In `CHANGELOG.md`, rename `Unreleased` to the version and date, and start a
   new empty `Unreleased` section above it.
3. Open a pull request titled `chore(release): <version>` and merge it.
4. Tag the merge commit on `main` and push the tag:

   ```bash
   git switch main
   git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```

5. Approve the `pypi` environment in the workflow run if it asks.

Then check that it installs from PyPI:

```bash
uvx chatlore@0.1.0 --version
uvx chatlore demo
```

A version number can be published only once. If something is wrong after
publishing, fix it and release the next patch version.
