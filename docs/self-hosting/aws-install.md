# Install on AWS with one command (beta)

`deploy/aws-installer/controlvar-aws` is a script with two commands. `install` runs the steps of [AWS CDK Deployment](cdk.md) for you, in your own AWS account, and creates the first administrator. `uninstall` removes what `install` made and shows what AWS keeps.

!!! warning "Beta: one real run so far, and it bills your AWS account"
    The installer runs this site's AWS CDK steps for you, and removes what it made. As of v0.37.0 it has been run end to end once, on a new account, and its options and messages may still change. From the moment you confirm, it creates resources that AWS bills for, until you uninstall. With everything up they cost about $10 a day, at us-west-2 list prices on 2026-10-17. It is not for production data. There are no upgrades. To move to a new release, uninstall and install again.

The installer is in releases from v0.37.0 on. It installs the core profile in `us-west-2`, in the shape `staging` has in [What Gets Deployed](cdk.md#what-gets-deployed), and the platform answers at a name you choose in your own Route 53 zone. It does not install the demo apps. To try the platform with demo data, use the [Quick Start](../getting-started/quick-start.md).

---

## What you need

| You need | How to check it |
|----------|-----------------|
| An AWS account that you administer. A new, empty one is best | Its ID is in the AWS console, and `aws sts get-caller-identity` prints it once you are signed in |
| An IAM Identity Center profile in your AWS CLI, with AdministratorAccess | `aws configure sso` makes one, and this page calls it `controlvar-install`. `aws sso login --profile controlvar-install` signs in |
| A public Route 53 hosted zone in that account, delegated so that the internet resolves it | `dig +short NS example.com @1.1.1.1` prints the zone's four name servers |
| An email address that receives mail | Send it a message |

If you have no IAM Identity Center profile yet, AWS's guides to [setting it up](https://docs.aws.amazon.com/singlesignon/latest/userguide/getting-started.html) and to [signing in with it from the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html) come first.

Run the installer from a Mac or a Linux computer, in a terminal. Install these tools on it: AWS CLI 2.28.24 or later, Node.js 22 or later with npm, Python 3.11 or later, jq 1.7.1 or later, cosign 3.1.3 or later, crane 0.22.1 or later, git, dig, curl and openssl. Let your network reach AWS, ghcr.io, Sigstore, npm, PyPI and 1.1.1.1.

### What the installer checks

Before it creates anything, the installer makes these checks. If one fails it says why, creates nothing and exits with code 2.

- Every tool above is installed, and none is older than the version given.
- The profile signs in through IAM Identity Center and no other way. A profile with access keys, a `credential_process`, a role to assume or a source profile is refused, and the installer reads no access keys from your shell.
- The sign-in was renewed in the last 15 minutes, and it belongs to the account you typed with `--account`.
- The account holds no stack whose name starts with `experimentation-`, in any region the account has enabled. If the account denies the installer the look in a region other than `us-west-2`, that region is shown as not checked.
- None of the names the install uses for its two repositories and two secrets is taken already, a secret waiting for deletion included.
- The installer can read your account in `us-west-2`, `us-east-1` and the global services. An account that answers AccessDenied to one of those reads is refused, because uninstall could not then show what the install left.
- Your sign-in is allowed to do what the install does. The installer asks IAM to simulate those actions, and refuses if one is denied.
- The Fargate vCPU quota is at least 3. Below 11 the installer warns that the API cannot scale out fully.
- The name you pass with `--domain` is one name directly below exactly one public Route 53 zone of the account, such as `app.example.com` in the zone `example.com`. The zone's four name servers answer from 1.1.1.1, and nothing exists at the name. The top of the zone, a name with two parts or more, a private zone and a name at or under example.com, example.net or example.org are refused.
- `--alarm-email` and `--admin-email` are not at example.com, example.net or example.org, and the first administrator's address is one the platform's sign-in accepts.
- `~/.cdk.json` is absent or holds only `{}`, because the CDK reads every setting in it as a default. Move it aside while the installer runs.
- This computer holds no record of a started install in this account and region.
- The folder is the unchanged tag of a release, because the installer installs that release's published images.

### If you do not have a zone yet

The installer writes only into a public Route 53 hosted zone in the account you install into, and only at a name below the top of the zone. If you have none, make a zone for the install and delegate it from the domain you own. AWS lists a hosted zone at $0.50 a month, and does not charge for one that is deleted within 12 hours of its creation. AWS records the certificate's name in public Certificate Transparency logs, so choose a name you are happy to have public, and uninstall does not remove it from them.

In the account you install into, make the zone. The block stops if a zone of that name already exists, and writes the zone's four name servers to `zone-ns.txt`:

```{.bash skip reason="aws: creates a public hosted zone in your AWS account"}
export AWS_PROFILE=controlvar-install
ZONE_NAME=install.example.com
COUNT=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME" \
  --query "length(HostedZones[?Name=='$ZONE_NAME.'])" --output text)
if [ "$COUNT" = 0 ]; then
  aws route53 create-hosted-zone --name "$ZONE_NAME" \
    --caller-reference "controlvar-$(date -u +%Y%m%dT%H%M%SZ)" \
    --hosted-zone-config '{"Comment": "Controlvar install", "PrivateZone": false}' \
    --query 'DelegationSet.NameServers[]' --output text | tr '\t' '\n' | LC_ALL=C sort > zone-ns.txt
  cat zone-ns.txt
else
  printf 'A zone named %s already exists.\n' "$ZONE_NAME"
fi
```

Then delegate the zone from the domain you own. If that domain is in a Route 53 hosted zone, in this account or another, sign in to the account that holds it, set `AWS_PROFILE` to a profile for that account and `PARENT_ZONE_ID` to the zone's ID, and add one NS record with the four name servers. The change batch uses `CREATE`, so it cannot replace a record that exists, and the block stops unless the batch holds one change with four servers:

```{.bash skip reason="aws: adds the delegation record to the parent hosted zone"}
export AWS_PROFILE=parent-account-profile
PARENT_ZONE_ID=Z0PARENTZONEID
ZONE_NAME=install.example.com
jq -n --arg name "$ZONE_NAME." --rawfile ns zone-ns.txt \
  '{Comment: "Controlvar install: delegate the zone", Changes: [{Action: "CREATE",
    ResourceRecordSet: {Name: $name, Type: "NS", TTL: 300,
      ResourceRecords: ($ns | split("\n") | map(select(length > 0)) | map({Value: .}))}}]}' \
  > delegation-create.json
if jq -e '(.Changes | length) == 1 and (.Changes[0].ResourceRecordSet.ResourceRecords | length) == 4' \
  delegation-create.json > /dev/null; then
  CHANGE_ID=$(aws route53 change-resource-record-sets --hosted-zone-id "$PARENT_ZONE_ID" \
    --change-batch file://delegation-create.json --query ChangeInfo.Id --output text)
  aws route53 wait resource-record-sets-changed --id "$CHANGE_ID"
else
  printf 'The change batch does not hold one change with four name servers.\n'
fi
```

If the domain is with another DNS provider, add an NS record for `install.example.com` there, with the same four name servers. Do not ask a public resolver for the name before the change is in place: it may keep the answer "not found" for up to 15 minutes. Then check that the internet sees the delegation:

```{.bash skip reason="aws: needs the zone you made above, which is in your AWS account"}
dig +short NS "$ZONE_NAME" @1.1.1.1
```

It prints the same four name servers. Use a name directly below the zone, such as `app.install.example.com`, as `--domain`.

---

## What it creates and costs

In your account, in `us-west-2`, `install` creates:

- the CDK's setup stack, `CDKToolkit`, if the account has none;
- two image repositories, with the signed release images copied into them after the installer verifies their signatures with cosign;
- two secrets, the API's JWT secret and the first administrator's password;
- one certificate for your name, validated by a DNS record in your zone;
- the nine `staging` stacks of the core profile ([What Gets Deployed](cdk.md#what-gets-deployed)): a network with one NAT gateway, Aurora PostgreSQL, Redis, the API and the dashboard behind a load balancer, and alarms that email you;
- two records in your zone, one for the certificate's validation and one that points your name at the load balancer;
- the ECS service-linked role, if the account has none;
- the first administrator, whose password the installer makes;
- a test flag in the new install, made by the installer's last check, which also makes an API key and revokes it.

The installer sets no budget alert. Set one in AWS Budgets if you want a warning before the bill does.

### Names you will see

The names say `staging` because the installer builds `staging`'s shape, whatever you call your install. In the AWS console you will find the stacks `experimentation-<stack>-staging`, the secrets under `/staging/experimentation/`, the image repositories `experimentation-platform/backend` and `experimentation-platform/web`, the ECS cluster and load balancer `experimentation-staging`, and the alarm topic `experimentation-alerts-staging`.

### Cost

With everything up, the resources cost about $0.42 an hour, or about $10 a day, at us-west-2 list prices on 2026-10-17. Aurora, Redis, the NAT gateway, the endpoints, the load balancer and the Fargate tasks bill by the hour whether or not anyone uses the platform. These are list prices, not your bill.

---

## Install

Get the release you will install. The installer runs only from a release's tag, unchanged:

```{.bash skip reason="checkout: clones the repository and enters it; the runner starts inside a checkout"}
git clone https://github.com/Controlvar/controlvar.git
cd controlvar
git switch --detach "v$(cat VERSION)"
```

Sign in just before you install. The installer refuses a sign-in that was not renewed in the last 15 minutes, and says so before it changes anything:

```{.bash skip reason="aws: signs in to your AWS account"}
aws sso login --profile controlvar-install
```

### The command

```{.bash exec}
./deploy/aws-installer/controlvar-aws --help
```
<!-- expect: beta -->
<!-- expect: --account -->
<!-- expect: --domain -->
<!-- expect: --alarm-email -->
<!-- expect: --admin-email -->
<!-- expect: --aws-profile -->
<!-- expect: --plan-only -->
<!-- expect: --yes -->

`--help` says that the installer is a beta. It lists these options and no others:

| Option | Used with | What it takes |
|--------|-----------|---------------|
| `--account ID` | `install`, `uninstall` | Required. The 12-digit ID of the account you mean, typed by you. The installer refuses the run if your sign-in is for another account |
| `--domain HOST` | `install` | Required. The name the platform will answer at, directly below a public Route 53 zone of the account, not the top of the zone |
| `--alarm-email ADDRESS` | `install` | Required. The address AWS emails when an alarm fires |
| `--admin-email ADDRESS` | `install` | The first administrator's sign-in. It is the alarm address when you do not pass it |
| `--aws-profile NAME` | `install`, `uninstall` | An IAM Identity Center profile with AdministratorAccess. It is `AWS_PROFILE` when you do not pass it |
| `--plan-only` | `install`, `uninstall` | Run the checks, print the plan and stop. It creates and deletes nothing in your AWS account, and exits 0 if every check passes |
| `--yes` | `install`, `uninstall` | Confirm without typing the account ID. The plan still prints |

Any other option is refused with exit code 2 before the installer changes anything. There is no `--region`, `--profile` or `--size`: the beta installs the core profile, in `staging`'s shape, in `us-west-2` only.

Type the account ID yourself. Do not fill `--account` from your sign-in, with `aws sts get-caller-identity` or any other command: the installer compares the two, and a command that fills one from the other checks nothing.

### Check first, then install

Run the installer with `--plan-only` first. It makes every check, prints what the install would create and what it would cost, and stops. Replace the example values with yours. The installer refuses the account, the name and the address as they are written here.

```{.bash skip reason="aws: reads your AWS account and creates nothing"}
./deploy/aws-installer/controlvar-aws install --plan-only \
  --account 123456789012 \
  --domain app.example.com \
  --alarm-email you@example.com \
  --aws-profile controlvar-install
```

Then run the same command without `--plan-only`. It prints the plan again and asks you to type the account ID. The prompt never times out. Without a terminal, pass `--yes` to confirm without typing.

```{.bash skip reason="aws: creates the install in your AWS account, which AWS bills"}
./deploy/aws-installer/controlvar-aws install \
  --account 123456789012 \
  --domain app.example.com \
  --alarm-email you@example.com \
  --aws-profile controlvar-install
```

The plan looks like this. The values are examples:

```text
Controlvar AWS installer 0.37.0 (beta). Checks (read-only; nothing is created yet):
  Tools (aws, git, python3 3.11, node, jq, dig, cosign, crane); release v0.37.0's tag, its CDK copied to the record ... ok
  Signed in with controlvar-install (IAM Identity Center) as AWSReservedSSO_AdministratorAccess_1a2b3c4d to account
  123456789012, as --account says. Region us-west-2, the only one the beta installs in.
  Allowed to create what it needs; no Controlvar stacks, repositories or secrets; Fargate vCPU limit 6 (needs 3) . ok
  Zone example.com: public, in this account, the one the internet uses; nothing at app.example.com; release
  images signed by Controlvar's release workflow .............................................................. ok
This creates in account 123456789012, us-west-2 (staging's shape, core profile): the CDK's setup stack, 2 image
  repositories, 2 secrets, a certificate, nine stacks (a network with one NAT gateway, Aurora, Redis, the API and the
  dashboard behind a load balancer, alarms emailing you@example.com), and two records in example.com: app.example.com
  and the certificate's. First administrator: you@example.com, password made here, shown once. Audit log: unsigned (core).
Cost: about $10 a day ($0.42 an hour) until you uninstall, used or not (us-west-2 list prices of 2026-10-17, not your
  bill). About 40 minutes. Not for production data: no upgrades. Record: ~/.controlvar-aws/123456789012-us-west-2/
Type the account ID shown above to create these resources:
```

The installer then prints one line for each of its nine steps, with the time the step took, and a line a minute while AWS creates the stacks. [What has been tested](#what-has-been-tested) has the times of the one real run, for install and for uninstall. The installer prints plain lines: no colour, no redrawing and no spinner. Errors go to standard error, and everything else to standard output.

Keep your computer awake and online until it finishes. If you press Ctrl-C, AWS carries on creating the stacks that it started, and the installer stops with exit code 130. Run `uninstall` next.

### Where it keeps its record

The installer keeps a record of the install in `~/.controlvar-aws/123456789012-us-west-2/`, with the account ID and `us-west-2` in the folder's name. It holds what the installer made, the CDK copied from the release's tag, and a log of each run. The log holds your account ID and email address, so remove them before you share it, and it holds no password, token or key.

Uninstall needs this record and this release's files. Keep the folder, and the clone, until you uninstall. The record belongs to one computer: run `uninstall` on the computer that ran `install`.

---

## After the install

When the install is done, the installer prints the address, the sign-in and the password of the first administrator:

```text
Controlvar 0.37.0 (beta) is installed and answering at:
  https://app.example.com
  Sign in   you@example.com
  Password  q7Rk2mXv9TbW4nLp8sJd3hYc
            Shown once. Also in Secrets Manager: /staging/experimentation/first-superuser-password
  Cost      about $10 a day until you uninstall it, used or not
Next: change the password (More > Change password); add a second administrator (Admin > Users > Invite User);
  confirm the alarm email, or alarms reach nobody:
  https://docs.controlvar.com/self-hosting/aws-install/#confirm-the-alarm-email
  If the address does not open yet, wait up to 15 minutes: your computer may hold an earlier "not found".
To remove it (keep this folder and the record folder until then; uninstall works from both):
  ./deploy/aws-installer/controlvar-aws uninstall --account 123456789012 --aws-profile controlvar-install
Log: ~/.controlvar-aws/123456789012-us-west-2/logs/20261022T101500Z-install.log (holds your account ID and email: remove them to share it)
```

The installer makes the password and shows it once, on your terminal. It never writes the password to the log. Without a terminal, the installer prints the `aws secretsmanager get-secret-value` command that reads it instead.

1. Open the address and sign in with that email and password.
2. Change the password: More > Change password. The copy in Secrets Manager is the first password, and it does not follow the change, so do not rely on it afterwards.
3. Add a second administrator, so the install does not depend on one account. Open Admin > Users > Invite User, and choose the role Admin (superuser).
4. Confirm the alarm email, below.

If the address does not open at once, wait up to 15 minutes. Your computer may hold an earlier answer that the name does not exist.

### Confirm the alarm email

The alarms do nothing until you confirm the subscription. SNS emails a confirmation link to your alarm address. Until it is confirmed, the alarms fire and nobody is told, and the link does not last for ever, so confirm it the same day. The installer says this in its summary too.

Do not just click the link: a subscription confirmed that way can be cancelled by the unsubscribe link in any alarm email, by anyone the email reaches. Instead, copy the `Token=` value out of the link and confirm it with `--authenticate-on-unsubscribe true`, after which unsubscribing takes an authenticated AWS call:

```{.bash skip reason="aws: confirms the alarm subscription in your AWS account"}
export AWS_PROFILE=controlvar-install
export AWS_REGION=us-west-2
TOKEN_FROM_THE_LINK='paste the Token= value here'
TOPIC_ARN=$(aws sns list-topics --output text \
  --query "Topics[?ends_with(TopicArn, ':experimentation-alerts-staging')].TopicArn")
aws sns confirm-subscription --topic-arn "$TOPIC_ARN" \
  --token "$TOKEN_FROM_THE_LINK" --authenticate-on-unsubscribe true
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
  --query "Subscriptions[].[Protocol,Endpoint,SubscriptionArn]" --output text
```

The last command must show the `email` subscription with a real ARN, not `PendingConfirmation`.

---

## Uninstall

Uninstall deletes the install and everything in it, its data included. It keeps no backup. It deletes only what `install` made: your zone, its other records and everything else in the account are left alone.

Sign in again, then run it from the computer that ran `install`, in the clone you installed from:

```{.bash skip reason="destructive: deletes the install and its data"}
aws sso login --profile controlvar-install
./deploy/aws-installer/controlvar-aws uninstall \
  --account 123456789012 \
  --aws-profile controlvar-install
```

`--plan-only` works here too: it makes the checks, prints the plan and deletes nothing. The plan says "Deletes the install in account 123456789012, us-west-2, at https://app.example.com", then lists what it deletes, with its data and with no backup kept. It lists what it keeps, such as the zone and, when it was there before the install, the CDK's setup stack. It says how long the deletion takes, and asks you to type the account ID.

Uninstall then runs six steps:

1. It deletes the record that points your name at the load balancer.
2. It deletes the nine stacks. Billing falls to zero as the stacks go.
3. It deletes the certificate and its validation record.
4. It deletes the secrets, the image repositories, the log groups and the task definitions that the install made.
5. It deletes the CDK's setup stack and its bucket, when the install made them.
6. It compares your account with the record made before the install.

At the end it prints what the account holds now that it did not hold before. These are the things AWS keeps, and nothing else, when the report says `Not explained: none`; the counts and dates here are examples:

```text
Uninstalled. The account now, against the record made before the install (2026-10-22 UTC):
Left by AWS; the installer cannot remove these, and they cost nothing or AWS removes them on the date shown:
  5 service-linked roles (AWSServiceRoleForECS and 4 more), kept for the account's later use
  4 AWS managed keys (aws/acm, aws/lambda, aws/s3, aws/secretsmanager); the database's key, deleted 2026-11-21
  3 DynamoDB backups of the deleted tables, deleted 2026-11-26
  the alarm subscription, never confirmed: AWS still lists it, with nothing to unsubscribe
  3 task definitions and 8 tag-index entries AWS is still clearing (minutes to hours)
Not explained: none. (Anything here is left alone: someone else may have made it while the install ran.)
https://docs.controlvar.com/self-hosting/aws-install/#remove-the-zone
```

[What `cdk destroy` leaves behind and bills](cdk.md#what-cdk-destroy-leaves-behind-and-bills) says what each of these is. If the report names anything under `Not explained`, the installer has left it alone, because someone else may have made it. Look at it before you delete it.

### Remove the zone

The installer never deletes your zone. If you made a zone only for this install, delete it after uninstall: first the delegation record in the domain you own, then the zone. Route 53 refuses to delete a zone that still holds records other than its own NS and SOA records.

First, in the account you installed into, read the zone's four name servers:

```{.bash skip reason="aws: reads the hosted zone in your AWS account"}
export AWS_PROFILE=controlvar-install
ZONE_NAME=install.example.com
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME" \
  --query "HostedZones[?Name=='$ZONE_NAME.' && Config.PrivateZone==\`false\`].Id" --output text)
ZONE_ID=${ZONE_ID#/hostedzone/}
aws route53 get-hosted-zone --id "$ZONE_ID" --query 'DelegationSet.NameServers[]' --output text \
  | tr '\t' '\n' | LC_ALL=C sort > zone-ns.txt
cat zone-ns.txt
```

If the domain you own is in a Route 53 hosted zone, sign in to the account that holds it, and set `AWS_PROFILE` to a profile for that account. The next block reads the delegation record back, compares its four name servers with the ones above, and deletes the record only when they are the same, so it cannot delete a record that someone else changed:

```{.bash skip reason="aws: deletes the delegation record from the parent hosted zone"}
export AWS_PROFILE=parent-account-profile
PARENT_ZONE_ID=Z0PARENTZONEID
ZONE_NAME=install.example.com
aws route53 list-resource-record-sets --hosted-zone-id "$PARENT_ZONE_ID" \
  --query "ResourceRecordSets[?Name=='$ZONE_NAME.' && Type=='NS']" --output json > delegation-now.json
jq -r '.[0].ResourceRecords[].Value' delegation-now.json | sed 's/\.$//' | LC_ALL=C sort > delegation-now-ns.txt
if [ "$(jq length delegation-now.json)" = 1 ] && diff zone-ns.txt delegation-now-ns.txt; then
  jq '{Changes: [{Action: "DELETE", ResourceRecordSet: .[0]}]}' delegation-now.json > delegation-delete.json
  CHANGE_ID=$(aws route53 change-resource-record-sets --hosted-zone-id "$PARENT_ZONE_ID" \
    --change-batch file://delegation-delete.json --query ChangeInfo.Id --output text)
  aws route53 wait resource-record-sets-changed --id "$CHANGE_ID"
else
  printf 'The delegation record is not the one this install made. Nothing was deleted.\n'
fi
```

With another DNS provider, delete the NS record for `install.example.com` there. Then, in the account you installed into, delete the zone:

```{.bash skip reason="aws: deletes the hosted zone from your AWS account"}
export AWS_PROFILE=controlvar-install
ZONE_NAME=install.example.com
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME" \
  --query "HostedZones[?Name=='$ZONE_NAME.' && Config.PrivateZone==\`false\`].Id" --output text)
ZONE_ID=${ZONE_ID#/hostedzone/}
aws route53 delete-hosted-zone --id "$ZONE_ID" --query ChangeInfo.Status --output text
```

### If uninstall stops at the network

If the installer finds a stack in `DELETE_FAILED`, it stops, names the stack and says what to do. The network can fail to delete when GuardDuty, with runtime monitoring for Fargate turned on in your account, has put its own interface endpoint and a managed group, whose name starts with `GuardDutyManaged`, in the network. The installer did not make them, so it does not delete them.

The message names the endpoint and the group by ID and prints the commands that list and delete them. Check that they are the only two things it names, delete those two yourself, and run `uninstall` again. It continues with the network. Anything else in the list is not GuardDuty's: do not delete it, and look at what it is first.

---

## If something goes wrong

The installer's exit code tells you whether anything was made:

| Code | Means | What to do |
|------|-------|------------|
| 0 | Done. After `uninstall`, the account holds only what AWS keeps | Nothing |
| 1 | Failed after something was made. The message lists what exists and what it bills | Run `uninstall`, then `install` again. If `uninstall` fails, its message ends "run uninstall again; it continues" |
| 2 | Refused, and nothing was changed. A declined confirmation is 2 as well | Read the message, fix the cause and run the command again. It is safe to repeat |
| 130 | You pressed Ctrl-C, or the installer was stopped. AWS carries on with the stacks it started | Run `uninstall` |

The installer uses no other exit code. Every message has three parts: what happened, what exists in your account and what it bills, and the next step. The first word names the code: `REFUSED` for 2, `FAILED` for 1 and `STOPPED` for 130.

**Install never continues.** If an install stops after it made something, it does not pick up where it stopped. Run `uninstall`, then `install` again. An `install` on a computer that holds the record of a started install refuses with exit code 2, reads your account and changes nothing, until you have run `uninstall`.

**Uninstall continues.** Uninstall can be run again from any half-built state. It deletes what is left of the install and nothing else.

**A sign-in that stops working.** The installer checks your sign-in again before it creates the nine stacks and before it deletes them. If it has stopped working, the installer stops with exit code 1 and says so. Sign in again, then run `uninstall` after an `install`, or run `uninstall` again after an `uninstall`.

**A lost record.** Uninstall needs the record folder. On a computer without it, `uninstall` refuses and deletes nothing, and points you at the computer that made the install or at [Destroying an environment](cdk.md#destroying-an-environment) to remove the stacks by hand. [Removing what is left](cdk.md#removing-what-is-left) covers the rest.

---

## What the beta does not do

- It installs in `us-west-2` only, and only the core profile.
- It needs a public Route 53 zone in the same account. It does not handle a zone in another account, other DNS providers, or the top of a zone.
- It does not continue an install that failed, and it does not upgrade one.
- It does not install the demo apps. Use the [Quick Start](../getting-started/quick-start.md) for those.
- It does not estimate the cost before you start, beyond the figures in the plan, and it sets no budget alert.

---

## What has been tested

CI runs every step and every refusal of the installer against a fake AWS CLI, on Linux and on macOS. The same CI run checks that the fake prints what the real AWS CLI prints.

The one real run:

- As of v0.37.0, the installer has run end to end once: on `<RUN: date>`, on a new AWS account in us-west-2, from a Mac, with release v0.36.2 and the installer from commit `<RUN: run head>`.
- As of v0.37.0, install took `<RUN: install minutes>` minutes in that run, and the first administrator signed in with the password it printed.
- As of v0.37.0, uninstall took `<RUN: uninstall minutes>` minutes in that run, and a comparison of the account with its state before the install found only what AWS keeps.
- As of v0.37.0, that run also stopped an install with Ctrl-C while the images were copied, ran `uninstall`, and `<RUN: part-way result>`.
- As of v0.37.0, the files under `deploy/aws-installer/` are the ones that ran.

As of v0.37.0, the installer has not been run on AWS in another region, with a zone in another account, through an uninstall that stops at the network, through a sign-in that ends in the middle of a run, or on a computer running Linux.
