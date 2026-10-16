# Responding to an incident on your deployment

You run your own Controlvar deployment, so your team runs its incident
response. This page points to the two things the project provides for that.

## If the cause may be a vulnerability in Controlvar

Report it privately, as [SECURITY.md](https://github.com/Controlvar/controlvar/blob/main/SECURITY.md)
describes: through GitHub private vulnerability reporting, or by email. Do not
open a public issue. SECURITY.md also says what happens after you report and on
what timeline; this page adds nothing to it.

## If a deploy caused it

Roll back to the last known-good release with the
[rollback runbook](../deployment/rollback-runbook.md). It covers the GitHub
Actions rollback, the AWS CLI rollback for when Actions is unavailable, and a
database restore.
