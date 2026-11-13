"""Request bodies for warehouse connections.

A connection names a warehouse and the read-only identity the platform uses
there, plus the limits every query is held to.  The body is the only place a
credential can arrive: no route takes one as a query or path parameter, and
the one secret a body can carry (a BigQuery service-account key) is
write-only -- it is encrypted before it is stored and no response returns it.
Snowflake connections carry no secret at all: the platform generates the key
pair and shows only the public half.

Every string field has a maximum length and a pattern; the pattern is applied
by Pydantic as a match of the whole value.  The per-warehouse checks that need
more than a pattern (the BigQuery key's contents, the Athena region list, the
Snowflake role list) run in the connection service.  Every model forbids
fields it does not list, so ``output_location``, ``external_id``, a password
or anything else unlisted is refused with 422.

A role-based connection (Amazon Athena, Amazon Redshift) names an IAM role
under the path ``/controlvar-warehouse/`` in the customer's account
(:data:`ROLE_ARN_PATTERN`); a role anywhere else is refused with 422.  An
Amazon Redshift connection names exactly one of a Serverless workgroup
(``workgroup_name``) or a provisioned cluster (``cluster_identifier``), and a
database user (``db_user``) with a cluster and only then.  It has no test
before it is saved: its role's trust policy names the external ID generated
when it is saved.

The operator ceilings (``WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS``,
``WAREHOUSE_MAX_BYTES_PER_QUERY``, ``WAREHOUSE_MAX_RUNS_PER_DAY``) are checked
by the service, where the settings are read; the bounds here are the fixed
lower limits and an outer upper bound.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from modules.backend.app.schemas.warehouse_sources import NAME_PATTERN

#: ``MYORG-MYACCOUNT``: the organisation-account identifier only.
SNOWFLAKE_ACCOUNT_PATTERN = r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9_]{1,255}$"
SNOWFLAKE_OBJECT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$"
BIGQUERY_PROJECT_PATTERN = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
BIGQUERY_LOCATION_PATTERN = r"^[A-Za-z]+(-[a-z]+[0-9]*)*$"
#: The characters of a JSON key file: printable ASCII and the three whitespace
#: characters a pasted file has between its fields.
SERVICE_ACCOUNT_JSON_PATTERN = r"^[\t\n\r\x20-\x7e]+$"
AWS_REGION_PATTERN = r"^[a-z]{2}(-[a-z]+)+-[0-9]{1,2}$"
#: A role under the IAM path ``/controlvar-warehouse/``, which every role-based
#: connection's role is under, by IAM's role-name characters (1-64).
ROLE_ARN_PATTERN = (
    r"^arn:aws:iam::[0-9]{12}:role/controlvar-warehouse/[A-Za-z0-9+=,.@_-]{1,64}$"
)
#: ``ROLE_ARN_PATTERN`` compiled, for code that checks an ARN outside a request
#: body (``aws.assume_role_session``).
ROLE_ARN_RE = re.compile(ROLE_ARN_PATTERN)
ATHENA_WORKGROUP_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"
ATHENA_DATABASE_PATTERN = r"^[a-z0-9_]{1,255}$"
#: A Redshift Serverless workgroup name: 3-64 lower-case letters, digits and
#: hyphens.
REDSHIFT_WORKGROUP_PATTERN = r"^[a-z0-9-]{3,64}$"
#: A provisioned cluster identifier: a lower-case letter, then up to 62
#: lower-case letters, digits and hyphens.
REDSHIFT_CLUSTER_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
#: A database name as an unquoted Redshift identifier, which Redshift folds to
#: lower case: a letter or _, then letters, digits, _ and $ (1-127).
REDSHIFT_DATABASE_PATTERN = r"^[a-z_][a-z0-9_$]{0,126}$"
#: The database user a cluster connection signs in as (the Data API's
#: ``DbUser``): a lower-case letter, then lower-case letters, digits and
#: _ + . @ - (1-64).
REDSHIFT_DB_USER_PATTERN = r"^[a-z][a-z0-9_+.@-]{0,63}$"

#: The largest pasted service-account key, in characters (16 KiB).
MAX_SERVICE_ACCOUNT_JSON_CHARS = 16 * 1024

ConnectionName = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=NAME_PATTERN)
]
SnowflakeAccount = Annotated[
    str, StringConstraints(max_length=300, pattern=SNOWFLAKE_ACCOUNT_PATTERN)
]
SnowflakeObject = Annotated[
    str, StringConstraints(max_length=255, pattern=SNOWFLAKE_OBJECT_PATTERN)
]
BigQueryProject = Annotated[
    str, StringConstraints(max_length=30, pattern=BIGQUERY_PROJECT_PATTERN)
]
BigQueryLocation = Annotated[
    str, StringConstraints(max_length=64, pattern=BIGQUERY_LOCATION_PATTERN)
]
ServiceAccountJson = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=MAX_SERVICE_ACCOUNT_JSON_CHARS,
        pattern=SERVICE_ACCOUNT_JSON_PATTERN,
    ),
]
AwsRegion = Annotated[str, StringConstraints(max_length=32, pattern=AWS_REGION_PATTERN)]
RoleArn = Annotated[str, StringConstraints(max_length=600, pattern=ROLE_ARN_PATTERN)]
AthenaWorkgroup = Annotated[
    str, StringConstraints(max_length=128, pattern=ATHENA_WORKGROUP_PATTERN)
]
AthenaDatabase = Annotated[
    str, StringConstraints(max_length=255, pattern=ATHENA_DATABASE_PATTERN)
]
RedshiftWorkgroup = Annotated[
    str, StringConstraints(max_length=64, pattern=REDSHIFT_WORKGROUP_PATTERN)
]
RedshiftCluster = Annotated[
    str, StringConstraints(max_length=63, pattern=REDSHIFT_CLUSTER_PATTERN)
]
RedshiftDatabase = Annotated[
    str, StringConstraints(max_length=127, pattern=REDSHIFT_DATABASE_PATTERN)
]
RedshiftDbUser = Annotated[
    str, StringConstraints(max_length=64, pattern=REDSHIFT_DB_USER_PATTERN)
]

#: Seconds; the operator ceiling is checked by the service.
QueryTimeout = Annotated[StrictInt, Field(ge=10, le=86400)]
#: Bytes; Athena's workgroup cutoff cannot be lower than 10 MB.
BytesPerQuery = Annotated[StrictInt, Field(ge=10_000_000, le=10**15)]
RunsPerDay = Annotated[StrictInt, Field(ge=1, le=10_000)]

_FORBID = ConfigDict(extra="forbid")


class _Limits(BaseModel):
    model_config = _FORBID

    name: ConnectionName
    query_timeout_seconds: QueryTimeout = 300
    max_runs_per_day: RunsPerDay = 20


class _SnowflakeFields(_Limits):
    warehouse_type: Literal["snowflake"]
    account: SnowflakeAccount
    user: SnowflakeObject
    role: SnowflakeObject
    warehouse: SnowflakeObject


class _BigQueryFields(_Limits):
    warehouse_type: Literal["bigquery"]
    billing_project: BigQueryProject
    location: BigQueryLocation
    max_bytes_per_query: BytesPerQuery


class _AthenaFields(_Limits):
    warehouse_type: Literal["athena"]
    region: AwsRegion
    role_arn: RoleArn
    workgroup: AthenaWorkgroup
    database: AthenaDatabase
    max_bytes_per_query: BytesPerQuery


class _RedshiftFields(_Limits):
    """Serverless (``workgroup_name``) or provisioned (``cluster_identifier``
    and ``db_user``), never both.  A Redshift query is capped in time only,
    so there is no ``max_bytes_per_query``."""

    warehouse_type: Literal["redshift"]
    region: AwsRegion
    role_arn: RoleArn
    workgroup_name: Optional[RedshiftWorkgroup] = None
    cluster_identifier: Optional[RedshiftCluster] = None
    db_user: Optional[RedshiftDbUser] = None
    database: RedshiftDatabase

    @model_validator(mode="after")
    def _one_shape(self) -> "_RedshiftFields":
        if (self.workgroup_name is None) == (self.cluster_identifier is None):
            raise ValueError(
                "Name exactly one of workgroup_name (Serverless) and "
                "cluster_identifier (a provisioned cluster)."
            )
        if (self.cluster_identifier is None) != (self.db_user is None):
            raise ValueError(
                "db_user goes with cluster_identifier: a provisioned cluster "
                "needs it, and a Serverless workgroup takes none."
            )
        return self


class SnowflakeConnectionCreate(_SnowflakeFields):
    """A Snowflake connection.  The platform generates the key pair."""


class BigQueryConnectionCreate(_BigQueryFields):
    """A BigQuery connection, with the service account's JSON key (write-only)."""

    service_account_json: ServiceAccountJson


class AthenaConnectionCreate(_AthenaFields):
    """An Amazon Athena connection.  It stores no secret."""


class RedshiftConnectionCreate(_RedshiftFields):
    """An Amazon Redshift connection.  It stores no secret."""


ConnectionCreate = Annotated[
    Union[
        SnowflakeConnectionCreate,
        BigQueryConnectionCreate,
        AthenaConnectionCreate,
        RedshiftConnectionCreate,
    ],
    Field(discriminator="warehouse_type"),
]


class SnowflakeConnectionUpdate(_SnowflakeFields):
    """``PUT /connections/{id}``: every non-secret field, as on create."""


class BigQueryConnectionUpdate(_BigQueryFields):
    """``PUT /connections/{id}``; a new key replaces the stored one, and
    leaving ``service_account_json`` out keeps it."""

    service_account_json: Optional[ServiceAccountJson] = None


class AthenaConnectionUpdate(_AthenaFields):
    """``PUT /connections/{id}`` for an Athena connection."""


class RedshiftConnectionUpdate(_RedshiftFields):
    """``PUT /connections/{id}`` for an Amazon Redshift connection."""


ConnectionUpdate = Annotated[
    Union[
        SnowflakeConnectionUpdate,
        BigQueryConnectionUpdate,
        AthenaConnectionUpdate,
        RedshiftConnectionUpdate,
    ],
    Field(discriminator="warehouse_type"),
]


class BigQueryConnectionTest(_BigQueryFields):
    """``POST /connections/test``: a BigQuery connection checked before it is
    saved.  The key is held in memory for the check and never stored."""

    service_account_json: ServiceAccountJson


class AthenaConnectionTest(_AthenaFields):
    """``POST /connections/test`` for an Athena connection."""


#: Snowflake has no test-before-save: its key pair is generated when the
#: connection is created, so there is nothing to sign in with before that.
#: Nor has Amazon Redshift: its role's trust policy names the external ID
#: generated when the connection is created, so a ``redshift`` body here is
#: refused with 422.
ConnectionTest = Annotated[
    Union[BigQueryConnectionTest, AthenaConnectionTest],
    Field(discriminator="warehouse_type"),
]
