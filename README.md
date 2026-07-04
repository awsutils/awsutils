# awsutils

Basic AWS CLI v2 extension using AWS CLI aliases. It adds:

```sh
aws utils hello
aws utils cloudwatch create-dashboard
aws utils inspect create-inspect-job
aws utils inspect describe-inspect-job --job-id <job-id>
aws utils inspect describe-inspect-job
aws utils logs create-fix-job
aws utils logs describe-fix-job --job-id <job-id>
aws utils s3 create-log-bucket
aws utils s3 create-fix-job
aws utils s3 describe-fix-job --job-id <job-id>
aws utils vpc create-fix-job
aws utils vpc describe-fix-job --job-id <job-id>
```

Expected output:

```text
Hello from aws utils!
```

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/awsutils/awsutils/main/install.sh | sh
```

The installer downloads the source archive from GitHub without using `git` and registers this alias in `~/.aws/cli/alias`:

```ini
[toplevel]
utils = !PYTHONPATH=$HOME/.awsutils/src python3 -m awsutils.cli
```

Then run:

```sh
aws utils hello
```

## Docker Web Shell

Build an image that includes `awsutils`, AWS CLI, and a browser terminal bound to `0.0.0.0:8080` inside the container:

```sh
docker build -t awsutils-webshell .
```

Pushes publish a multi-architecture `linux/amd64` and `linux/arm64` image to `ghcr.io/awsutils/awsutils`.

Run it locally:

```sh
docker run --rm -p 8080:8080 \
  -e WEBSHELL_CREDENTIAL='admin:change-me' \
  awsutils-webshell
```

Open `http://localhost:8080` and run:

```sh
awsutils hello
aws utils hello
```

To use local AWS configuration without hiding the container's AWS CLI alias file, mount config and credentials files individually:

```sh
docker run --rm -p 8080:8080 \
  -e WEBSHELL_CREDENTIAL='admin:change-me' \
  -v "$HOME/.aws/config:/home/awsutils/.aws/config:ro" \
  -v "$HOME/.aws/credentials:/home/awsutils/.aws/credentials:ro" \
  awsutils-webshell
```

`WEBSHELL_CREDENTIAL` is optional, but exposing an unauthenticated shell on a reachable interface is unsafe. The container also supports `WEBSHELL_HOST`, `WEBSHELL_PORT`, and `WEBSHELL_SHELL`.

## Inspect Jobs

Start an AWS best-practice inspection in the background:

```sh
aws utils inspect create-inspect-job
```

The command installs the matching `bptools` binary from `https://awsutils.github.io/bptools/` into `~/.awsutils/bin`, starts it in the background, and prints JSON containing a `job_id`.

Describe the job and retrieve captured result data:

```sh
aws utils inspect describe-inspect-job --job-id <job-id>
```

List all known inspect jobs:

```sh
aws utils inspect describe-inspect-job
```

Optional filters are passed through to `bptools`:

```sh
aws utils inspect create-inspect-job --services ec2,s3,iam
aws utils inspect create-inspect-job --ids ec2-imdsv2-check
aws utils inspect create-inspect-job --concurrency 8 --no-prefetch
```

`describe-inspect-job` always returns JSON. If `bptools` emits JSON to stdout, it is parsed into `best_practice_result`; raw captured stdout and stderr are included as `stdout_text` and `stderr_text`.

## CloudWatch Logs Fix Jobs

Start a background job that applies tags, enables native log-group deletion protection, and sets retention to 7 days for every CloudWatch Logs log group in the current region:

```sh
aws utils logs create-fix-job
```

Customize region, retention, tags, or parallelism:

```sh
aws utils logs create-fix-job --region us-east-1
aws utils logs create-fix-job --retention-days 14 --tag Environment=prod --tag Owner=platform
aws utils logs create-fix-job --max-workers 16
```

Describe a job or list all known jobs:

```sh
aws utils logs describe-fix-job --job-id <job-id>
aws utils logs describe-fix-job
```

## CloudWatch Dashboards

Create the bundled full and simple dashboards:

```sh
aws utils cloudwatch create-dashboard
```

Dashboard creation runs in parallel when more than one dashboard is selected, and failed downloads or AWS CLI calls are retried up to five times.

Create one dashboard:

```sh
aws utils cloudwatch create-dashboard --dashboard simple
aws utils cloudwatch create-dashboard --dashboard full --name my-dashboard
aws utils cloudwatch create-dashboard --max-workers 2
```

## S3 Utilities

Create or update the shared log bucket used for VPC Flow Logs, ALB access logs, and S3 server access logs:

```sh
aws utils s3 create-log-bucket
aws utils s3 create-log-bucket --bucket logbucket-123456789012 --region us-east-1
```

Start a background job that fixes all existing S3 buckets by enabling versioning, tagging, intelligent tiering, lifecycle policy, required bucket policy, and server access logging to the shared log bucket:

```sh
aws utils s3 create-fix-job
aws utils s3 create-fix-job --log-bucket logbucket-123456789012 --tag Environment=prod
```

Describe a job or list all known jobs:

```sh
aws utils s3 describe-fix-job --job-id <job-id>
aws utils s3 describe-fix-job
```

## VPC Fix Jobs

Start a background job that repairs missing VPC networking components, enables common VPC endpoints, and enables S3 VPC Flow Logs to the shared S3 log bucket for every VPC in the current region:

```sh
aws utils vpc create-fix-job
```

The job processes VPCs and endpoint creation in parallel. Failed AWS CLI calls are retried up to five times. Network repair can create internet gateways, public/private subnets, route tables, Elastic IPs, one NAT gateway per AZ, VPC endpoints, security groups, S3 buckets, and flow logs.

Limit the job to selected VPCs or a region:

```sh
aws utils vpc create-fix-job --vpc-ids vpc-123,vpc-456 --region us-east-1
aws utils vpc create-fix-job --max-workers 12
```

Describe a job or list all known jobs:

```sh
aws utils vpc describe-fix-job --job-id <job-id>
aws utils vpc describe-fix-job
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
