"""What differs between environments, decided in one place (#139, #142).

Three rules live here, and every stack asks this module rather than comparing
``env_name`` itself. Before this file each stack had its own spelling --
``RETAIN if env != "dev"`` in four places, ``SNAPSHOT`` everywhere in one,
``RETAIN`` unconditionally in six -- so a staging teardown left sixteen billed
or name-blocking resources behind and nobody had decided that it should.

**Retention.** ``prod`` keeps its data when a stack is destroyed; every other
environment (``dev``, ``staging``, ``demo``) is disposable and leaves nothing
behind. That is the whole rule: one environment's data is worth keeping, the
rest exist to be torn down and rebuilt, and a rebuilt staging must not collide
with the named resources its previous incarnation retained.

**Sizing.** ``prod`` gets the redundant shape (two NAT gateways, a writer and a
reader); every other environment gets one of each (DECISIONS D7 for the NAT
gateway, T24 for staging's single Aurora instance).

**The API's settings.** No environment runs the API with development
settings, which are for a local trial: the API and migration task definitions
carry ``APP_ENV=prod`` in ``prod`` and ``APP_ENV=staging`` everywhere else,
``dev`` and ``demo`` included.
``backend/tests/unit/infrastructure/test_cdk_api_settings.py`` builds the
application's settings from each environment's synthesized task definitions.

``infrastructure/tests/test_environments_do_not_collide.py`` asserts the
consequences on a real synth: the exact DeletionPolicy of every resource that
has one, per environment, and the exact instance and NAT gateway counts.
"""

from __future__ import annotations

from aws_cdk import RemovalPolicy

#: The environments whose data outlives ``cdk destroy``. One, deliberately.
RETAINING_ENVIRONMENTS = frozenset({"prod"})


def retains_data(env_name: str) -> bool:
    """``True`` when this environment's stateful resources survive teardown."""
    return env_name in RETAINING_ENVIRONMENTS


def data_removal_policy(env_name: str) -> RemovalPolicy:
    """RETAIN in prod, DESTROY elsewhere.

    For Cognito, KMS, the named DynamoDB tables, the named ``/ecs/`` log
    groups, the S3 buckets, the Kinesis stream and the OpenSearch domain.
    """
    return RemovalPolicy.RETAIN if retains_data(env_name) else RemovalPolicy.DESTROY


def database_removal_policy(env_name: str) -> RemovalPolicy:
    """SNAPSHOT in prod, DESTROY elsewhere -- for the Aurora cluster.

    A final snapshot is itself a billed resource that ``cdk destroy`` leaves
    behind, which is the right trade for production and the wrong one for an
    environment that exists to be rebuilt.
    """
    return RemovalPolicy.SNAPSHOT if retains_data(env_name) else RemovalPolicy.DESTROY


#: Days of Aurora automated backups, and so of point-in-time restore (#391,
#: DECISIONS D38). 35 is the most Aurora allows.
AURORA_BACKUP_RETENTION_DAYS = {"prod": 35, "staging": 35}


def aurora_backup_retention_days(env_name: str) -> int:
    """35 days of Aurora backups in prod and staging; 1 everywhere else (#391).

    Set explicitly in every environment rather than left unset, which means
    CloudFormation's default of 1 day. Raising it updates the cluster in
    place; lowering it later discards the backups older than the new value.
    """
    return AURORA_BACKUP_RETENTION_DAYS.get(env_name, 1)


def nat_gateway_count(env_name: str) -> int:
    """Two in prod (one per AZ); one elsewhere (DECISIONS D7)."""
    return 2 if env_name == "prod" else 1


def aurora_instance_count(env_name: str) -> int:
    """Writer plus reader in prod; a single writer elsewhere (T24).

    Staging used to get the prod pair on ``db.t3.medium``: a second instance
    doubling the database bill of an environment whose job is to prove a
    deployment works, not to survive an AZ failure.
    """
    return 2 if env_name == "prod" else 1


def redis_node_count(env_name: str) -> int:
    """Nodes in the Redis replication group: a primary and two replicas in prod, one elsewhere.

    The Redis stack sizes the group with it, and the monitoring stack builds
    one CPU alarm per node from it: the nodes are ``<group id>-001`` to
    ``<group id>-00N`` and ElastiCache publishes host-level metrics per node
    (``CacheClusterId``), not per group. Staging runs one node (Stream I,
    I-1d; pinned by ``infrastructure/tests/test_redis_sizing.py``).
    """
    return 3 if env_name == "prod" else 1


def api_app_env(env_name: str) -> str:
    """The ``APP_ENV`` of the API and migration task definitions.

    ``prod`` in prod and ``staging`` in every other environment, so ``dev`` and
    ``demo`` run the API with staging settings. The application reads ``prod``
    as production and ``staging`` as staging, and both refuse placeholder
    secrets (``backend/app/core/config.py``).
    """
    return "prod" if env_name == "prod" else "staging"
