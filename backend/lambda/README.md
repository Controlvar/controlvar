# Lambda Functions (not deployed)

This directory contains AWS Lambda code for event processing and feature flag evaluation.
**No stack deploys it**; see `docs/integrations/aws.md` for the functions the stacks do deploy.
Experiment assignment happens only in the API (`POST /api/v1/tracking/assign`), which applies
the global holdout, mutual exclusion groups and targeting.

## 📁 Structure

```
backend/lambda/
├── shared/                    # Shared utilities and models
│   ├── __init__.py
│   ├── consistent_hash.py    # Consistent hashing for assignments
│   ├── models.py             # Pydantic data models
│   └── utils.py              # Common utilities (logging, AWS helpers)
│
├── event_processor/         # Event processor Lambda
│   ├── handler.py
│   ├── requirements.txt
│   └── tests/
│
└── feature_flag_evaluation/ # Feature flag evaluation Lambda
    ├── handler.py
    ├── requirements.txt
    └── tests/
```

## 🎯 Lambda Functions

### 1. Event Processor Lambda
**Purpose:** Process incoming events from Kinesis stream

**Performance Targets:**
- Batch processing: 100-500 events
- Processing latency: < 100ms per batch
- Error rate: < 0.1%

**Key Features:**
- Validates and enriches events
- Aggregates real-time metrics
- Archives to S3 via Firehose
- Handles partial batch failures

### 2. Feature Flag Evaluation Lambda
**Purpose:** Real-time feature flag evaluation with targeting

**Performance Targets:**
- P50 latency: < 15ms
- P99 latency: < 40ms
- Cache hit rate: > 95%

**Key Features:**
- Evaluates targeting rules
- Applies rollout percentages
- Caches flag configurations
- Records evaluation events

## 🔧 Shared Utilities

### Consistent Hashing (`consistent_hash.py`)

Implements MurmurHash3-style hashing for deterministic variant assignment:

```python
from shared import ConsistentHasher

hasher = ConsistentHasher()
variant = hasher.assign_variant(
    user_id="user_123",
    experiment_key="checkout_redesign",
    variants=[
        {"key": "control", "allocation": 0.5},
        {"key": "treatment", "allocation": 0.5},
    ],
    traffic_allocation=1.0,
)
# Returns: "control" or "treatment" deterministically
```

### Data Models (`models.py`)

Pydantic models for type safety and validation:

- `Assignment` - Experiment assignment data
- `ExperimentConfig` - Experiment configuration
- `FeatureFlagConfig` - Feature flag configuration
- `EventData` - Incoming event data
- `LambdaResponse` - Standard response format

### Utilities (`utils.py`)

Common helper functions:

- `get_logger()` - Structured JSON logging
- `validate_event()` - Event validation
- `format_response()` - Standard response formatting
- `put_dynamodb_item()` - DynamoDB operations
- `put_kinesis_record()` - Kinesis operations
- `get_env_variable()` - Environment variable management

## 🚀 Deployment

No stack deploys these functions. `cdk deploy --all` deploys the platform, and
`infrastructure/tests/test_lambda_functions_deployed.py` fails if a stack starts
deploying code from this directory.

## 📊 Monitoring

All Lambda functions emit metrics to CloudWatch:

- **Invocations** - Total function invocations
- **Errors** - Error count and rate
- **Duration** - Execution time (P50, P99)
- **ConcurrentExecutions** - Concurrent invocations
- **Custom Metrics** - Business-specific metrics

## 🧪 Testing

Run unit tests:

```bash
python -m pytest backend/lambda/event_processor
python -m pytest backend/lambda/feature_flag_evaluation
python -m pytest backend/lambda/shared
```

## 📝 Development Status

- [x] Phase 1: Setup & Infrastructure (COMPLETED)
  - [x] Directory structure created
  - [x] Shared utilities module
  - [x] Consistent hashing algorithm
  - [x] Data models and helpers

- The assignment function was removed (#480): assignment is the API's.
- [ ] Event Processor Lambda: code and tests, not deployed
- [ ] Feature Flag Lambda: code and tests, not deployed

## 📚 References

- [AWS Lambda Best Practices](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)
- [DynamoDB Best Practices](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html)
- [Kinesis Stream Processing](https://docs.aws.amazon.com/streams/latest/dev/building-consumers.html)
- EP-010 Ticket: `docs/tickets/EP-010-lambda-real-time-services.md`
