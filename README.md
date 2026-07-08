# awsutils

Basic AWS CLI v2 extension using AWS CLI aliases. It adds:

```sh
aws hello
aws cloudwatch create-dashboard
aws inspect create-inspect-job
aws inspect describe-inspect-job --job-id <job-id>
aws inspect describe-inspect-job
aws logs create-fix-job
aws logs describe-fix-job --job-id <job-id>
aws s3 create-log-bucket
aws s3 create-fix-job
aws s3 describe-fix-job --job-id <job-id>
aws vpc create-fix-job
aws vpc describe-fix-job --job-id <job-id>
```

Expected output:

```text
Hello!
```

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/awsutils/awsutils/main/install.sh | sh
```

The installer downloads the source archive from GitHub without using `git` and registers aliases in `~/.aws/cli/alias`:

```ini
[toplevel]
hello = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli hello
inspect = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli inspect
vpc = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli vpc

[command cloudwatch]
create-dashboard = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli cloudwatch create-dashboard

[command logs]
create-fix-job = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli logs create-fix-job
describe-fix-job = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli logs describe-fix-job

[command s3]
create-log-bucket = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli s3 create-log-bucket
create-fix-job = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli s3 create-fix-job
describe-fix-job = !PYTHONPATH=$HOME/.aws/cli/tools/src python3 -m awsutils.cli s3 describe-fix-job
```

Then run:

```sh
aws hello
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
  -p 2222:22 \
  -e WEBSHELL_CREDENTIAL='admin:change-me' \
  awsutils-webshell
```

Open `http://localhost:8080` and run:

```sh
aws hello
```

The web shell runs as the `ec2-user` user, which has passwordless `sudo` inside the container. The image also starts `sshd` on container port `22`; SSH login is enabled for `ec2-user` with an empty password for restricted-network deployments. You can still pass `SSHD_AUTHORIZED_KEYS` or mount `/home/ec2-user/.ssh/authorized_keys` to enable key-based SSH login. The image includes common shell tools such as `vim`, `nano`, `wget`, `curl`, `git`, `ssh`, `jq`, `less`, `ping`, `dig`, `nc`, `tar`, `zip`, and `unzip`.

To use local AWS configuration without hiding the container's AWS CLI alias file, mount config and credentials files individually:

```sh
docker run --rm -p 8080:8080 \
  -e WEBSHELL_CREDENTIAL='admin:change-me' \
  -v "$HOME/.aws/config:/home/ec2-user/.aws/config:ro" \
  -v "$HOME/.aws/credentials:/home/ec2-user/.aws/credentials:ro" \
  awsutils-webshell
```

`WEBSHELL_CREDENTIAL` is optional, but exposing an unauthenticated shell on a reachable interface is unsafe. The container also supports `WEBSHELL_HOST`, `WEBSHELL_PORT`, and `WEBSHELL_SHELL`.

## Inspect Jobs

Start an AWS best-practice inspection in the background:

```sh
aws inspect create-inspect-job
```

The command installs the matching inspection binary when needed, starts it in the background, and prints JSON containing a `job_id`.

Describe the job and retrieve captured result data:

```sh
aws inspect describe-inspect-job --job-id <job-id>
```

List all known inspect jobs:

```sh
aws inspect describe-inspect-job
```

Optional filters are passed through to the inspection binary:

```sh
aws inspect create-inspect-job --services ec2,s3,iam
aws inspect create-inspect-job --ids ec2-imdsv2-check
aws inspect create-inspect-job --concurrency 8 --no-prefetch
```

`describe-inspect-job` always returns JSON. If the inspection binary emits JSON to stdout, it is parsed into `best_practice_result`; raw captured stdout and stderr are included as `stdout_text` and `stderr_text`.

## CloudWatch Logs Fix Jobs

Start a background job that applies tags, enables native log-group deletion protection, and sets retention to 7 days for every CloudWatch Logs log group in the current region:

```sh
aws logs create-fix-job
```

Customize region, retention, tags, or parallelism:

```sh
aws logs create-fix-job --region us-east-1
aws logs create-fix-job --retention-days 14 --tag Owner=platform
aws logs create-fix-job --max-workers 16
```

Describe a job or list all known jobs:

```sh
aws logs describe-fix-job --job-id <job-id>
aws logs describe-fix-job
```

## CloudWatch Dashboards

Create the bundled full and simple dashboards:

```sh
aws cloudwatch create-dashboard
```

Dashboard creation runs in parallel when more than one dashboard is selected, and failed downloads or AWS CLI calls are retried up to five times.

Create one dashboard:

```sh
aws cloudwatch create-dashboard --dashboard simple
aws cloudwatch create-dashboard --dashboard full --name my-dashboard
aws cloudwatch create-dashboard --max-workers 2
```

## S3 Utilities

Create or update the shared log bucket used for VPC Flow Logs, ALB access logs, and S3 server access logs:

```sh
aws s3 create-log-bucket
aws s3 create-log-bucket --bucket logbucket-123456789012 --region us-east-1
```

Start a background job that fixes all existing S3 buckets by enabling versioning, tagging, intelligent tiering, lifecycle policy, required bucket policy, and server access logging to the shared log bucket:

```sh
aws s3 create-fix-job
aws s3 create-fix-job --log-bucket logbucket-123456789012 --tag Owner=platform
```

Describe a job or list all known jobs:

```sh
aws s3 describe-fix-job --job-id <job-id>
aws s3 describe-fix-job
```

## VPC Fix Jobs

Start a background job that repairs missing VPC networking components, enables common VPC endpoints, and enables S3 VPC Flow Logs to the shared S3 log bucket for every VPC in the current region:

```sh
aws vpc create-fix-job
```

The job processes VPCs and endpoint creation in parallel. Failed AWS CLI calls are retried up to five times. Network repair can create internet gateways, public/private subnets, route tables, Elastic IPs, one NAT gateway per AZ, VPC endpoints, security groups, S3 buckets, and flow logs.

Limit the job to selected VPCs or a region:

```sh
aws vpc create-fix-job --vpc-ids vpc-123,vpc-456 --region us-east-1
aws vpc create-fix-job --max-workers 12
```

Describe a job or list all known jobs:

```sh
aws vpc describe-fix-job --job-id <job-id>
aws vpc describe-fix-job
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
