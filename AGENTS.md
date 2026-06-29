# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

This is a small Python AWS CLI alias extension. It provides commands under:

```sh
aws utils ...
```

The installer registers an AWS CLI alias that runs:

```sh
python3 -m awsutils.cli
```

## Code Layout

- `awsutils/cli.py`: CLI parsing, help text, `hello`, and inspect/bptools job commands.
- `awsutils/cloudwatch.py`: CloudWatch dashboard creation utilities.
- `awsutils/logs.py`: CloudWatch Logs fix-job implementation.
- `awsutils/s3.py`: S3 log-bucket and fix-job implementation.
- `awsutils/vpc.py`: VPC fix-job implementation.
- `install.sh`: installer that configures the AWS CLI alias.
- `README.md`: user-facing usage documentation.

## Development Commands

Run syntax checks:

```sh
python3 -m py_compile awsutils/cli.py awsutils/cloudwatch.py awsutils/logs.py awsutils/s3.py awsutils/vpc.py
```

Smoke-test help output:

```sh
python3 -m awsutils.cli help
python3 -m awsutils.cli cloudwatch create-dashboard help
python3 -m awsutils.cli logs create-fix-job help
python3 -m awsutils.cli s3 create-log-bucket help
python3 -m awsutils.cli s3 create-fix-job help
python3 -m awsutils.cli vpc create-fix-job help
python3 -m awsutils.cli inspect create-inspect-job help
```

Smoke-test non-mutating describe commands:

```sh
python3 -m awsutils.cli vpc describe-fix-job
python3 -m awsutils.cli logs describe-fix-job
python3 -m awsutils.cli s3 describe-fix-job
python3 -m awsutils.cli inspect describe-inspect-job
```

## AWS Safety Notes

- `awsutils/vpc.py` can create or modify AWS resources, including subnets, internet gateways, route tables, Elastic IPs, NAT gateways, VPC endpoints, security groups, S3 buckets, and flow logs.
- `awsutils/logs.py` can modify CloudWatch Logs log groups by setting tags, deletion protection, and retention policies.
- `awsutils/s3.py` can create and modify S3 buckets, including bucket policies, versioning, lifecycle configuration, intelligent tiering, tags, encryption, public access blocks, and server access logging.
- Do not run mutating commands against a real AWS account during routine verification unless explicitly requested.
- Prefer help commands and describe/list commands for local smoke tests.
- The inspect/bptools path intentionally does not use the retry wrapper used by CloudWatch/VPC utilities.

## Implementation Notes

- Keep `cli.py` focused on argument parsing and dispatch.
- Put service-specific behavior in separate modules.
- Preserve JSON output contracts for job create/describe commands.
- Background jobs store state under `~/.awsutils` by default, or `AWSUTILS_INSTALL_DIR` when set.
- If syntax checks create `awsutils/__pycache__/`, remove it before committing.

## Git Notes

- Commit only intended source/documentation files.
- Do not commit generated files, local job state, credentials, or bytecode caches.
