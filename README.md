# awsutils

Basic AWS CLI v2 extension using AWS CLI aliases. It adds:

```sh
aws utils hello
aws utils cloudwatch create-dashboard
aws utils inspect create-inspect-job
aws utils inspect describe-inspect-job --job-id <job-id>
aws utils inspect describe-inspect-job
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

## VPC Fix Jobs

Start a background job that repairs missing VPC networking components, enables common VPC endpoints, and enables S3 VPC Flow Logs for every VPC in the current region:

```sh
aws utils vpc create-fix-job
```

The job processes VPCs and endpoint creation in parallel. Failed AWS CLI calls are retried up to five times. Network repair can create internet gateways, public/private subnets, route tables, Elastic IPs, NAT gateways, VPC endpoints, security groups, S3 buckets, and flow logs.

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
