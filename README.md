# awsutils

Basic AWS CLI v2 extension using AWS CLI aliases. It adds:

```sh
aws utils hello
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

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
