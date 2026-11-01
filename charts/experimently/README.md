# Controlvar Helm chart

This chart installs Controlvar on Kubernetes: the API, the dashboard and, by
default, a bundled PostgreSQL and Redis. Install it with the four blocks of
[Kubernetes (Helm)](https://docs.controlvar.com/self-hosting/kubernetes/):
the `chart-kind` workflow runs those blocks as written on a kind cluster, for
the `core` and `full` profiles, and runs `helm test` after the install.

## Resource names

The chart names what it creates after the release. With the guide's release,
`experimently` in the namespace `experimently`, the API's Deployment and
Service are `experimently-api` and the dashboard's are `experimently-web`. A
release whose name does not contain `experimently`, such as `prod`, gets
`prod-experimently-api` and `prod-experimently-web`.

## After the install

The API's `migrate` init container creates or upgrades the database schema
before the API starts. If a pod stays in `Init`, read that container's log; for
the guide's release:
`kubectl logs -n experimently deploy/experimently-api -c migrate`.

`helm test experimently -n experimently` checks that the API answers on your
public host and refuses another host name. The health probes do not check the
host name, so they stay healthy when `publicBaseUrl` is wrong.

Sign in at `publicBaseUrl` as `firstSuperuser`, with the
`FIRST_SUPERUSER_PASSWORD` of your Secret. That password is read only while the
database has no users: changing it later does nothing.

## Users' addresses

The Ingress sends API requests to the API's Service, so the API reads each
user's address from the `X-Forwarded-For` header your ingress controller adds.
It believes that header from the API pod's own network and from the addresses
and networks in `api.forwardedAllowIps`, which defaults to the private ranges,
`10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`. That covers an ingress controller on
a private pod network. To believe only your ingress controller, set it to the
network its pods get their addresses from, as
[Kubernetes (Helm)](https://docs.controlvar.com/self-hosting/kubernetes/#users-addresses)
shows. It cannot be empty. An entry that is not an address or a network stops
the `migrate` init container, and its log names the entry.

## Without the Ingress

With `ingress.enabled=false`, route `/api`, `/ws` and `/health` to the API's
Service (`experimently-api` for the guide's release) and everything else to the
dashboard's (`experimently-web`), as the chart's Ingress does. Pass the Host header
unchanged: the API answers only for the host name of `publicBaseUrl`.
Whatever reaches the API's Service must connect from an address in
`api.forwardedAllowIps` for the API to read users' addresses from its
`X-Forwarded-For`.

## The bundled PostgreSQL and Redis

They are for evaluation and small installs: each is one replica, the chart does
not back them up, and it never moves them to a new major version (PostgreSQL 16
and Redis 7). Back up the database before every upgrade.
