# Releasing

Publishing to PyPI is **irreversible per version**. A filename can never be
reused, even after you delete the release — so `0.1.0` uploaded by mistake means
the next release is `0.1.1`, forever. The workflow is built around that fact.

## One-time setup (you must do this; it needs your PyPI login)

Authentication is [Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
so there is no API token to create, store, or rotate. Instead PyPI is told which
repository and workflow are allowed to publish, and GitHub proves it with a
short-lived OIDC token.

Because `ctrl-data-mgmt` doesn't exist on either index yet, both are registered
as **pending publishers**.

**1. TestPyPI** — <https://test.pypi.org/manage/account/publishing/>

| field | value |
|---|---|
| PyPI Project Name | `ctrl-data-mgmt` |
| Owner | `cdmaestas` |
| Repository name | `ctrl-data-mgmt` |
| Workflow name | `release.yml` |
| Environment name | `testpypi` |

**2. PyPI** — <https://pypi.org/manage/account/publishing/>

Same values, except **Environment name** is `pypi`.

**3. GitHub environments** — Settings → Environments, create `testpypi` and
`pypi`. On `pypi`, add yourself under *Required reviewers*. That turns the final
upload into a button you press deliberately rather than something that happens
because you pushed a tag.

The environment names must match on both sides or the OIDC exchange is rejected.

## Doing a release

**Dry run first.** Actions → Release → Run workflow, leaving *Publish to
TestPyPI only* checked. This builds, tests, publishes to TestPyPI, then installs
that published wheel in a clean machine and actually runs `cdm scan`, `find`,
`du` and `doctor` against it. Nothing touches real PyPI.

**Then the real thing:**

```bash
# 1. Bump the version in pyproject.toml, commit it
# 2. Tag it -- the tag MUST be v<version> or the workflow refuses
git tag v0.1.0
git push origin v0.1.0
```

That runs verify → TestPyPI → smoke test → *(approval)* → PyPI → GitHub release.

## What the workflow refuses to do

- **Publish a tag that disagrees with `pyproject.toml`.** `v0.2.0` against
  version `0.1.0` fails in the first job, before anything is uploaded.
- **Publish something it didn't test.** One build produces the artifacts; every
  later job downloads those exact files. Rebuilding per job would mean the
  thing you tested is not the thing you shipped.
- **Publish an incomplete package.** The man page must be in the wheel, and the
  sdist must carry docs, licence and tests.
- **Publish from a manual run.** `workflow_dispatch` can only reach TestPyPI.

## After the first release

Once the project exists on PyPI, the pending publisher becomes a normal one —
nothing to change. Bump the version, tag, push.

The man page ships to `share/man/man1`, which lands inside the virtualenv on a
`pipx` install rather than on the system `MANPATH`. That is a packaging fact,
not a bug; `man ./man/cdm.1` works from a checkout.
