# Experimently Helm chart

This chart installs Experimently on Kubernetes: the API, the dashboard and, by
default, a bundled PostgreSQL and Redis. Install it with the four blocks of
[Kubernetes (Helm)](https://getexperimently.github.io/experimently/self-hosting/kubernetes/):
the `chart-kind` workflow runs those blocks as written on a kind cluster, for
the `core` and `full` profiles, and runs `helm test` after the install.

## After the install

The API's `migrate` init container creates or upgrades the database schema
before the API starts. If a pod stays in `Init`, read that container's log:
`kubectl logs -n <namespace> deploy/<release>-experimently-api -c migrate`.

`helm test <release> -n <namespace>` checks that the API answers on your
public host and refuses another host name. The health probes do not check the
host name, so they stay healthy when `publicBaseUrl` is wrong.

Sign in at `publicBaseUrl` as `firstSuperuser`, with the
`FIRST_SUPERUSER_PASSWORD` of your Secret. That password is read only while the
database has no users: changing it later does nothing.

## Without the Ingress

With `ingress.enabled=false`, route `/api`, `/ws` and `/health` to the
`<release>-experimently-api` Service and everything else to
`<release>-experimently-web`, as the chart's Ingress does. Pass the Host header
unchanged: the API answers only for the host name of `publicBaseUrl`.

## The bundled PostgreSQL and Redis

They are for evaluation and small installs: each is one replica, and the chart
does not back them up. Back up the database before every upgrade.
