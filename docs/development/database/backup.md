# Database Backup Schedules and Retention Policies

This page lists the backups the CDK stacks configure for each data store. The
recovery procedures are in the [Disaster Recovery Plan](../../deployment/disaster-recovery.md).

## Aurora PostgreSQL

### Backup Mechanism

Aurora PostgreSQL protects the cluster with:
1. **Automated backups**: a daily snapshot and continuous backup of the transaction log, which
   together allow point-in-time recovery to any moment within the retention period
2. **Manual snapshots**: `deploy.yml` takes one before every deploy's migration
   (`pre-deploy-<env>-<tag>-<time>`); they are kept until you delete them
3. **A final snapshot** when the prod database stack is deleted

### Backup and Maintenance Windows

The stack sets no preferred backup window and no preferred maintenance window, so Aurora
chooses both for the cluster's region.

### Retention Policy

| Environment | Automated Backup Retention | Point-in-Time Recovery | Manual Snapshot Retention |
|-------------|----------------------------|------------------------|---------------------------|
| Production  | 35 days                    | Up to 35 days          | Until manually deleted    |
| Staging     | 35 days                    | Up to 35 days          | Until manually deleted    |
| Any other   | 1 day                      | Up to 1 day            | Until manually deleted    |

### Implementation

The retention is what the stack sets: `aurora_backup_retention_days()` in
`infrastructure/cdk/stacks/environments.py` returns it per environment, and
`infrastructure/cdk/stacks/enhanced_database_stack.py` passes it to the cluster as
`backup=rds.BackupProps(retention=Duration.days(...))`. 35 days is the most Aurora allows.

## DynamoDB Tables

Every DynamoDB table the stacks create has point-in-time recovery on. No stack
schedules an on-demand backup.

## ElastiCache Redis

### Backup Mechanism

ElastiCache Redis uses snapshot-based backups:
1. **Automatic Snapshots**: Taken daily during the configured backup window

### Backup Windows

| Environment | Backup Window (UTC) | Maintenance Window (UTC) |
|-------------|---------------------|--------------------------|
| Development | 02:00-03:00         | Sun 04:00-05:00          |
| Staging     | 02:00-03:00         | Sun 04:00-05:00          |
| Production  | 02:00-03:00         | Sun 04:00-05:00          |

### Retention Policy

| Environment | Snapshot Retention |
|-------------|-------------------|
| Development | 1 day             |
| Staging     | 3 days            |
| Production  | 7 days            |

### Failover

In `prod` the replication group has three nodes and fails over automatically;
`staging` runs one node, with no failover.

## Encryption

- **Aurora PostgreSQL**: Backups are encrypted using the same KMS key as the database
- **ElastiCache Redis**: Snapshots are encrypted if the cluster has encryption at rest enabled
