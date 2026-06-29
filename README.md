# awsutils

Basic AWS CLI v2 extension using AWS CLI aliases. It adds:

```sh
aws utils hello
aws utils inspect create-inspect-job
aws utils inspect describe-inspect-job --job-id <job-id>
aws utils inspect describe-inspect-job
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

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
