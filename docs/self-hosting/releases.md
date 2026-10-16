# Releases, images and verification

Controlvar releases are cut by CI from a tag: `release.yml` builds, signs
and publishes each one. The version a running deployment reports in
`GET /api/v1/modules` is the string in the repository's `VERSION` file, and
each image's `org.opencontainers.image.version` label and the Helm chart's
`version` and `appVersion` are the same string. `deploy/compose/compose.yml`
names the images of its own release.

## What a release publishes

For version `X.Y.Z`, in the repository's GitHub Container Registry namespace:

| Image | Platforms |
|---|---|
| `…/controlvar:core-X.Y.Z` | `linux/amd64` |
| `…/controlvar:full-X.Y.Z` | `linux/amd64` |
| `…/controlvar-web:core-X.Y.Z` | `linux/amd64` |
| `…/controlvar-web:full-X.Y.Z` | `linux/amd64` |

A final release also moves `:core` and `:full` to point at it, and only once
everything else in the release, including the chart below, has succeeded.
A pre-release, a tag with a `-` in it such as `v1.2.0-rc.1`, publishes its
versioned tags only: `:core` and `:full` stay on the newest final release.

Every image is built for `linux/amd64` only; #181 tracks an `arm64` build.

Each image is scanned for stored credentials with trivy's secret scanner
before it is pushed, and a finding stops that image and the release. The
claim is that the scan found nothing, not that the image is proven to hold no
credential. There are two scans:

- **The whole image, with trivy's default rules.** Those rules skip
  `/usr/share` and `/usr/lib`, `vendor/` and `locale/` directories, `*.md`
  files, file names containing `example` or `_test`, and `node_modules/`
  (trivy skips that one even with every rule turned off).
- **This project's own files, with every rule turned off.** These are `/app`
  in the API images, and `/usr/share/nginx/html` and `/etc/nginx` in the
  dashboard images. They are copied out of the image without running it.

So the remaining gap is third-party files on the paths the default rules
skip. After the push, the release checks that the pushed image's layers are
the layers it scanned, before signing it.

The GitHub release also carries the Helm chart as `controlvar-X.Y.Z.tgz`,
packaged from the tagged tree after `helm lint --strict`. Before it is
attached, `scripts/check_chart_package.py` reads the `Chart.yaml` inside the
archive and requires its `version` and `appVersion` to be exactly `X.Y.Z`, so
the chart installs the images of the same release. The chart is a release
asset only; it is not pushed to a chart registry.

## Verifying what you pulled

Every image is signed with [cosign][cosign] keyless — the signature is bound to
the workflow's OIDC identity and recorded in Rekor, so there is no key to
leak — and carries an SPDX SBOM as a Sigstore attestation. The same SBOMs are
attached to the GitHub release.

!!! note "The SBOMs describe `linux/amd64`"

    That is the only platform published, so each SBOM describes the whole
    image.

You need [cosign][cosign], Docker with Buildx (`docker buildx version` prints
one) and `jq`. Nothing is pulled: every command below reads the registry, and
none needs an account.

The project was renamed from Experimently to Controlvar between two releases, and
an image's signature names the repository that built it. Releases up to 0.34.x
were published as `ghcr.io/getexperimently/experimently` and
`ghcr.io/getexperimently/experimently-web` and signed by
`https://github.com/getexperimently/experimently/.github/workflows/release.yml`.
Releases from 0.35.0 on are published as `ghcr.io/controlvar/controlvar` and
`ghcr.io/controlvar/controlvar-web` and signed by
`https://github.com/Controlvar/controlvar/.github/workflows/release.yml`.
Verify an image with the identity of the repository that built it; the other
one fails with `no matching signatures`. The commands below verify the newest
release under the former name.

Set `IMAGE` to the image you run. `:core` is the newest final release; a
versioned tag such as `:core-0.26.3` is one release, and `full` in place of
`core` is the full profile:

```{.bash exec}
IMAGE=ghcr.io/getexperimently/experimently:core
IDENTITY='^https://github\.com/getexperimently/experimently/\.github/workflows/release\.yml@refs/(heads/main|tags/v[0-9].*)$'
NO_LOGIN=$(mktemp -d)
```

`IDENTITY` names one workflow, this repository's `release.yml`. It accepts
`release.yml` run from `main`, which is how release-please cuts a release and
how `gh workflow run release.yml -f tag=vX.Y.Z` runs one again, and run from a
`v` tag, which is how a tag pushed by hand starts it. A signature made by any
other workflow, repository or branch fails.

`NO_LOGIN` is an empty directory. The two `cosign` commands run with
`DOCKER_CONFIG` pointing at it, so they read no registry login and show that
none is needed.

Check the signature:

```{.bash exec}
DOCKER_CONFIG=$NO_LOGIN cosign verify "$IMAGE" \
  --certificate-identity-regexp "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  | jq -r '.[].optional.Subject'
```
<!-- expect: https://github.com/getexperimently/experimently/.github/workflows/release.yml@refs/ -->

It prints the identity in the signing certificate, such as
`https://github.com/getexperimently/experimently/.github/workflows/release.yml@refs/heads/main`.
cosign writes the checks it made to stderr. When no signature matches, it
fails with `no matching signatures`.

Check the SBOM attestation the same way. It prints the attestation's predicate
type, `https://spdx.dev/Document`:

```{.bash exec}
DOCKER_CONFIG=$NO_LOGIN cosign verify-attestation --type spdxjson "$IMAGE" \
  --certificate-identity-regexp "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  | jq -r '.payload | @base64d | fromjson | .predicateType'
```
<!-- expect: https://spdx.dev/Document -->

Without the `jq`, it prints the whole attestation, SBOM included.

The image also states its own version and profile in its labels, which
`docker buildx imagetools inspect` reads from the registry without pulling the
image:

```{.bash exec}
docker buildx imagetools inspect "$IMAGE" --format '{{json .Image.Config.Labels}}' \
  | jq '{version: ."org.opencontainers.image.version", profile: ."io.experimently.profile"}'
```
<!-- expect: "version": " -->
<!-- expect: "profile": "core" -->

It prints the release's version, such as `"version": "0.26.3"`, and
`"profile": "core"`.

A running deployment reports the same two things, which is the quickest check
that it is running what you think:

```{.bash exec}
curl -s localhost:8000/api/v1/modules
```
<!-- expect: "profile":"core" -->

It prints `{"profile":"core","modules":[],"version":…}` on the core profile;
the full profile lists its modules.

[cosign]: https://docs.sigstore.dev/cosign/overview/

## SDK releases

Each SDK is released by its own tag, such as `sdk/js/v1.2.3` or
`sdk/python/v1.2.3`, and versions independently of the platform.
`sdk-release.yml` publishes to PyPI and npm by trusted publishing (OIDC — no
long-lived token in any secret), behind the `sdk-release` environment.
A prerelease — a version with a `-`, such as `1.0.0-rc.1` — publishes under
the `next` dist-tag, so `npm install <package>` keeps resolving the last
stable version and `npm install <package>@next` gets the prerelease.

How a maintainer cuts a release, and what to do when its image scan fails, is
in `CONTRIBUTING.md`, "Releasing".
