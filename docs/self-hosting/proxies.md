# Users' addresses behind a proxy

The API counts its rate limits per user address and writes that address on its
access lines. Behind a proxy, every connection the API receives comes from the
proxy, so the API reads the user's address from the `X-Forwarded-For` header the
proxy adds. It believes that header only from the proxies below.

## The setting

- The API always believes 127.0.0.1, ::1 and the networks its container is
  directly attached to. In Docker Compose that covers the dashboard container and
  a TLS proxy on the same host that connects to 127.0.0.1.
- `FORWARDED_ALLOW_IPS` adds proxies to that: IP addresses and networks,
  separated by commas, `10.0.0.5,10.0.0.0/16`. Write a network with its host bits
  zero, `10.0.0.0/16`, not `10.0.0.5/16`. Host names are not accepted. Unset or
  empty adds none.
- Set it in the container's environment. The entrypoint reads it there, not from
  `.env.prod`. Do not pass `--forwarded-allow-ips` in the container's command: the
  entrypoint adds its own after it, and that one wins.
- The API logs what it believes when it starts, for example:
  `[entrypoint] trusted proxies: 127.0.0.1 ::1; this container's networks: 172.19.0.0/16; FORWARDED_ALLOW_IPS: (none)`.
  When it cannot read its networks from `/proc`, the line begins
  `[entrypoint] WARNING`, and it believes 127.0.0.1, ::1 and the setting's entries
  only.
- An entry that is not an address or a network stops the container at start-up,
  with a line that names the entry's position, for example:
  `FORWARDED_ALLOW_IPS entry 2 ('10.0.0.5/16') is an address with a mask, which matches no proxy. Write the network, 10.0.0.0/16, or the address alone, 10.0.0.5.`
- `*` is refused in every environment. `0.0.0.0/0` and `::/0` believe the header
  from every connection, not only your proxy's, and staging and production refuse
  them.

## By install

| Install | What to set |
|---|---|
| Docker Compose (`deploy/compose`) | Nothing for a TLS proxy on the same host that connects to 127.0.0.1. For one on another machine, its address in `.env`; for one in a container on a Docker network of its own, that network's subnet: see [Users' addresses](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/README.md#users-addresses) in the Compose README. |
| Helm | `api.forwardedAllowIps`, by default the private ranges; you can narrow it to your ingress controller's pod network: see [Kubernetes (Helm)](kubernetes.md#users-addresses). |
| AWS (CDK) | Run `cdk deploy` for the Fargate stack before you deploy the image; the stack sets the VPC's range, `10.0.0.0/16` ([AWS CDK Deployment](cdk.md#users-addresses)). |
| The image behind another proxy | `FORWARDED_ALLOW_IPS`: the proxy's addresses or network, unless it is on a network the container is attached to. |

## Checking it

Each API request writes an access line whose `event` begins with the address the
API resolved. Count them for API requests, which leaves out the health checks.
For the image on its own, with the container's name in `API_CONTAINER`:

```{.bash skip reason="server: reads the logs of a running API container"}
docker logs "$API_CONTAINER" 2>&1 | grep '"uvicorn.access"' | grep ' /api/' \
  | sed -E 's/.*"event": "([^ ]*):[0-9]+ - .*/\1/' | sort | uniq -c | sort -rn | head
```

On Kubernetes, for the guide's release:

```{.bash skip reason="server: reads the logs of a running API pod"}
kubectl logs -n experimently deploy/experimently-api -c api | grep '"uvicorn.access"' \
  | grep ' /api/' | sed -E 's/.*"event": "([^ ]*):[0-9]+ - .*/\1/' | sort | uniq -c | sort -rn | head
```

The Compose README and [AWS CDK Deployment](cdk.md#users-addresses) give the same
count for their installs.

Each line is an address and the number of API requests from it. Your users'
addresses mean it works. If every request comes from one address that is a
proxy's, the setting does not cover that proxy, or the proxy in front of it sends
no `X-Forwarded-For`.

## Signs that it is wrong

- Signing in fails with "Too many attempts" for people who tried once.
- The dashboard, or an SDK, is answered `429 Too Many Requests` in ordinary use.
- Every access line shows the same address.
