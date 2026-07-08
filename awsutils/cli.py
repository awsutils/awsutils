import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import urlretrieve

from awsutils.cloudwatch import DEFAULT_ASSET_BASE_URL, create_cloudwatch_dashboard
from awsutils.logs import DEFAULT_RETENTION_DAYS, create_logs_fix_job, describe_logs_fix_job, run_logs_fix_job
from awsutils.s3 import create_log_bucket, create_s3_fix_job, describe_s3_fix_job, run_s3_fix_job
from awsutils.vpc import create_vpc_fix_job, describe_vpc_fix_job, run_vpc_fix_job


INSTALL_ROOT = Path(os.environ.get("AWSUTILS_INSTALL_DIR", Path.home() / ".aws" / "cli" / "tools"))
BPTOOLS_DIR = INSTALL_ROOT / "bin"
BPTOOLS_BINARY = BPTOOLS_DIR / ("bptools.exe" if os.name == "nt" else "bptools")
JOBS_DIR = INSTALL_ROOT / "inspect" / "jobs"
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class NoColorArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("color", False)
        try:
            super().__init__(*args, **kwargs)
        except TypeError:
            kwargs.pop("color", None)
            super().__init__(*args, **kwargs)


def _json_dump(data):
    print(json.dumps(data, indent=4, sort_keys=True))


def _public_job_state(state):
    hidden = {"stdout", "stderr", "bptools_binary", "bptools_args"}
    return {key: value for key, value in state.items() if key not in hidden}


def _show_help(argv):
    topic = [arg for arg in argv if arg not in {"help", "--help", "-h"}]
    if topic == []:
        print("""NAME
     aws - utility commands

DESCRIPTION
     Utility commands for AWS CLI.

SYNOPSIS
     aws <command> [parameters]

AVAILABLE COMMANDS
     * hello

     * cloudwatch

     * inspect

     * logs

     * s3

     * vpc
""")
        return 0
    if topic == ["cloudwatch"]:
        print("""NAME
     cloudwatch - CloudWatch utility commands

DESCRIPTION
     Create CloudWatch dashboards.

SYNOPSIS
     aws cloudwatch <command> [parameters]

AVAILABLE COMMANDS
     * create-dashboard
""")
        return 0
    if topic == ["cloudwatch", "create-dashboard"]:
        print("""NAME
     create-dashboard - Create CloudWatch dashboards

SYNOPSIS
     aws cloudwatch create-dashboard
          [--dashboard full|simple|all]
          [--name <value>]
          [--base-url <value>]
          [--max-workers <value>]
""")
        return 0
    if topic == ["inspect"]:
        print("""NAME
     inspect - Run AWS best-practice inspections

DESCRIPTION
     Installs and runs inspection jobs in the background.

SYNOPSIS
     aws inspect <command> [parameters]

AVAILABLE COMMANDS
     * create-inspect-job

     * describe-inspect-job
""")
        return 0
    if topic == ["inspect", "create-inspect-job"]:
        print("""NAME
     create-inspect-job - Create an inspect job

DESCRIPTION
     Installs the inspection binary when needed and starts an AWS best-practice
     inspection job in the background.

SYNOPSIS
     aws inspect create-inspect-job
          [--ids <value>]
          [--services <value>]
          [--concurrency <value>]
          [--no-prefetch]
          [--force-install]

OPTIONS
     --ids (string)
          Comma-separated check IDs to run.

     --services (string)
          Comma-separated AWS services to inspect.

     --concurrency (integer)
          Maximum concurrent checks.

     --no-prefetch (boolean)
          Disable inspection cache prefetching.

     --force-install (boolean)
          Download the inspection binary even if it already exists.
""")
        return 0
    if topic == ["inspect", "describe-inspect-job"]:
        print("""NAME
     describe-inspect-job - Describe inspect jobs

DESCRIPTION
     Returns inspect job state as JSON. When --job-id is omitted, all known jobs
     are listed.

SYNOPSIS
     aws inspect describe-inspect-job
          [--job-id <value>]

OPTIONS
     --job-id (string)
          Inspection job ID returned by create-inspect-job. Omit to list all jobs.
""")
        return 0
    if topic == ["logs"]:
        print("""NAME
     logs - CloudWatch Logs utility commands

DESCRIPTION
     Start and describe background CloudWatch Logs fix jobs.

SYNOPSIS
     aws logs <command> [parameters]

AVAILABLE COMMANDS
     * create-fix-job

     * describe-fix-job
""")
        return 0
    if topic == ["logs", "create-fix-job"]:
        print("""NAME
     create-fix-job - Create a CloudWatch Logs fix job

DESCRIPTION
     Starts a background job that applies tags, enables log-group deletion
     protection, and sets retention for every CloudWatch Logs log group.

SYNOPSIS
     aws logs create-fix-job
          [--region <value>]
          [--retention-days <value>]
          [--tag Key=Value]
          [--max-workers <value>]
""")
        return 0
    if topic == ["logs", "describe-fix-job"]:
        print("""NAME
     describe-fix-job - Describe CloudWatch Logs fix jobs

SYNOPSIS
     aws logs describe-fix-job
          [--job-id <value>]
""")
        return 0
    if topic == ["s3"]:
        print("""NAME
     s3 - S3 utility commands

DESCRIPTION
     Create shared log buckets and start S3 bucket fix jobs.

SYNOPSIS
     aws s3 <command> [parameters]

AVAILABLE COMMANDS
     * create-log-bucket

     * create-fix-job

     * describe-fix-job
""")
        return 0
    if topic == ["s3", "create-log-bucket"]:
        print("""NAME
     create-log-bucket - Create an S3 log bucket

DESCRIPTION
     Creates or updates a shared S3 log bucket and bucket policy allowing VPC
     Flow Logs, ALB access logs, and S3 server access logs delivery.

SYNOPSIS
     aws s3 create-log-bucket
          [--bucket <value>]
          [--region <value>]
""")
        return 0
    if topic == ["s3", "create-fix-job"]:
        print("""NAME
     create-fix-job - Create an S3 bucket fix job

DESCRIPTION
     Starts a background job that enables versioning, tags, intelligent tiering,
     lifecycle policy, required bucket policy, and server access logging for all
     existing S3 buckets.

SYNOPSIS
     aws s3 create-fix-job
          [--region <value>]
          [--log-bucket <value>]
          [--tag Key=Value]
          [--max-workers <value>]
""")
        return 0
    if topic == ["s3", "describe-fix-job"]:
        print("""NAME
     describe-fix-job - Describe S3 fix jobs

SYNOPSIS
     aws s3 describe-fix-job
          [--job-id <value>]
""")
        return 0
    if topic == ["vpc"]:
        print("""NAME
     vpc - VPC utility commands

DESCRIPTION
     Start and describe background VPC fix jobs.

SYNOPSIS
     aws vpc <command> [parameters]

AVAILABLE COMMANDS
     * create-fix-job

     * describe-fix-job
""")
        return 0
    if topic == ["vpc", "create-fix-job"]:
        print("""NAME
     create-fix-job - Create a VPC fix job

DESCRIPTION
     Starts a background job that repairs missing VPC networking components,
     then enables common VPC endpoints and S3 VPC Flow Logs for selected VPCs,
     or all VPCs in the current region.

SYNOPSIS
     aws vpc create-fix-job
          [--vpc-ids <value>]
          [--region <value>]
          [--max-workers <value>]
""")
        return 0
    if topic == ["vpc", "describe-fix-job"]:
        print("""NAME
     describe-fix-job - Describe VPC fix jobs

SYNOPSIS
     aws vpc describe-fix-job
          [--job-id <value>]
""")
        return 0
    return None


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_ansi(text):
    return ANSI_ESCAPE_RE.sub("", text)


def _bptools_url():
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        os_name = "darwin"
    elif system == "linux":
        os_name = "linux"
    elif system == "windows":
        os_name = "windows"
    else:
        raise RuntimeError(f"unsupported operating system: {system}")

    if machine in {"x86_64", "amd64"}:
        arch = "amd64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        raise RuntimeError(f"unsupported CPU architecture: {machine}")

    suffix = ".exe" if os_name == "windows" else ""
    host = "aws" + "utils.github.io"
    return f"https://{host}/bptools/bptools-{os_name}-{arch}{suffix}"


def _ensure_bptools(force=False):
    if BPTOOLS_BINARY.exists() and not force:
        return str(BPTOOLS_BINARY)

    BPTOOLS_DIR.mkdir(parents=True, exist_ok=True)
    url = os.environ.get("AWSUTILS_BPTOOLS_URL", _bptools_url())
    try:
        urlretrieve(url, BPTOOLS_BINARY)
    except Exception:
        raise RuntimeError("could not download inspection binary") from None
    if os.name != "nt":
        BPTOOLS_BINARY.chmod(0o755)
    return str(BPTOOLS_BINARY)


def _job_paths(job_id):
    job_dir = JOBS_DIR / job_id
    return {
        "dir": job_dir,
        "state": job_dir / "job.json",
        "stdout": job_dir / "stdout.log",
        "stderr": job_dir / "stderr.log",
    }


def _build_bptools_args(args):
    out = []
    if args.ids:
        out.extend(["-ids", args.ids])
    if args.services:
        out.extend(["-services", args.services])
    if args.concurrency is not None:
        out.extend(["-concurrency", str(args.concurrency)])
    if args.no_prefetch:
        out.extend(["-prefetch=false"])
    return out


def _create_inspect_job(args):
    binary = _ensure_bptools(force=args.force_install)
    _clear_inspect_jobs()
    job_id = str(uuid.uuid4())
    paths = _job_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    bptools_args = _build_bptools_args(args)
    state = {
        "job_id": job_id,
        "status": "PENDING",
        "created_at": _utc_now(),
        "bptools_binary": binary,
        "bptools_args": bptools_args,
        "stdout": str(paths["stdout"]),
        "stderr": str(paths["stderr"]),
    }
    _write_json(paths["state"], state)

    cmd = [
        sys.executable,
        "-m",
        "awsutils.cli",
        "_run-inspect-job",
        job_id,
        binary,
        *bptools_args,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    state["status"] = "RUNNING"
    state["pid"] = proc.pid
    state["started_at"] = _utc_now()
    _write_json(paths["state"], state)
    _json_dump(_public_job_state(state))
    return 0


def _parse_stdout(stdout_text):
    text = stdout_text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _parse_bptools_text(text)


def _parse_bptools_text(text):
    lines = text.splitlines()
    result = {"summary": {}, "rules": []}
    current = None

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("rules_with_issues="):
            for part in stripped.split():
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                try:
                    result["summary"][key] = int(value)
                except ValueError:
                    result["summary"][key] = value
            continue

        rule_match = re.match(r"^([A-Za-z0-9_.:/+=,@-]+) \(([^)]+)\)$", stripped)
        if rule_match:
            current = {"check_id": rule_match.group(1), "counts": {}, "findings": []}
            for part in rule_match.group(2).split():
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                try:
                    current["counts"][key] = int(value)
                except ValueError:
                    current["counts"][key] = value
            result["rules"].append(current)
            continue

        if current is None:
            continue

        if stripped.startswith("description:"):
            current["description"] = stripped.removeprefix("description:").strip()
            continue

        if stripped.startswith("docs:"):
            current["docs"] = stripped.removeprefix("docs:").strip()
            continue

        finding_match = re.match(r"^\[([^\]]+)\]\s+(.+?)(?:\s+[—-]\s+(.+))?$", stripped)
        if finding_match:
            finding = {
                "status": finding_match.group(1),
                "resource_id": finding_match.group(2).strip(),
            }
            if finding_match.group(3):
                finding["message"] = finding_match.group(3).strip()
            current["findings"].append(finding)

    if result["summary"] or result["rules"]:
        return result
    return None


def _inspect_job_details(job_id, include_logs=True):
    paths = _job_paths(job_id)
    if not paths["state"].exists():
        return {"job_id": job_id, "status": "NOT_FOUND"}

    state = _read_json(paths["state"])
    stdout_text = _read_clean_text(paths["stdout"])
    stderr_text = _read_clean_text(paths["stderr"])
    best_practice_result = _parse_stdout(stdout_text)
    state["best_practice_result"] = best_practice_result
    if include_logs:
        state["stdout_text"] = stdout_text
        state["stderr_text"] = stderr_text
    else:
        if isinstance(best_practice_result, dict) and "rules" in best_practice_result:
            state["best_practice_result"] = {
                "summary": best_practice_result.get("summary", {}),
                "rule_count": len(best_practice_result.get("rules", [])),
                "rules_with_issues": [
                    {
                        "check_id": rule.get("check_id"),
                        "counts": rule.get("counts", {}),
                        "description": rule.get("description"),
                        "docs": rule.get("docs"),
                    }
                    for rule in best_practice_result.get("rules", [])
                ],
            }
        state["stdout_bytes"] = len(stdout_text.encode("utf-8"))
        state["stderr_bytes"] = len(stderr_text.encode("utf-8"))
    return _public_job_state(state)


def _list_inspect_jobs():
    jobs = []
    if JOBS_DIR.exists():
        for state_path in sorted(JOBS_DIR.glob("*/job.json")):
            jobs.append(_inspect_job_details(state_path.parent.name, include_logs=False))
    return {"jobs": jobs}


def _clear_inspect_jobs():
    if JOBS_DIR.exists():
        shutil.rmtree(JOBS_DIR)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _describe_inspect_job(args):
    if not args.job_id:
        _json_dump(_list_inspect_jobs())
        return 0

    state = _inspect_job_details(args.job_id)
    _json_dump(state)
    return 1 if state["status"] == "NOT_FOUND" else 0


def _run_inspect_job(args):
    paths = _job_paths(args.job_id)
    state = _read_json(paths["state"])
    state["status"] = "RUNNING"
    state["runner_pid"] = os.getpid()
    state.setdefault("started_at", _utc_now())
    _write_json(paths["state"], state)

    env = os.environ.copy()
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
    with paths["stdout"].open("wb") as stdout, paths["stderr"].open("wb") as stderr:
        proc = subprocess.run([args.binary, *args.bptools_args], stdout=stdout, stderr=stderr, env=env)

    stdout_text = _read_clean_text(paths["stdout"])
    state["completed_at"] = _utc_now()
    state["exit_code"] = proc.returncode
    state["status"] = "SUCCEEDED" if proc.returncode == 0 else "FAILED"
    state["best_practice_result"] = _parse_stdout(stdout_text)
    _write_json(paths["state"], state)
    return proc.returncode


def _read_clean_text(path):
    if not path.exists():
        return ""
    return _strip_ansi(path.read_text(encoding="utf-8", errors="replace"))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_run-inspect-job":
        run_parser = NoColorArgumentParser(prog="aws _run-inspect-job")
        run_parser.add_argument("command")
        run_parser.add_argument("job_id")
        run_parser.add_argument("binary")
        run_parser.add_argument("bptools_args", nargs=argparse.REMAINDER)
        return _run_inspect_job(run_parser.parse_args())

    if len(sys.argv) > 1 and sys.argv[1] == "_run-vpc-fix-job":
        run_parser = NoColorArgumentParser(prog="aws _run-vpc-fix-job")
        run_parser.add_argument("command")
        run_parser.add_argument("job_id")
        run_parser.add_argument("--vpc-ids")
        run_parser.add_argument("--region")
        run_parser.add_argument("--max-workers", type=int, default=8)
        return run_vpc_fix_job(run_parser.parse_args())

    if len(sys.argv) > 1 and sys.argv[1] == "_run-logs-fix-job":
        run_parser = NoColorArgumentParser(prog="aws _run-logs-fix-job")
        run_parser.add_argument("command")
        run_parser.add_argument("job_id")
        run_parser.add_argument("--region")
        run_parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
        run_parser.add_argument("--tag", action="append", default=[])
        run_parser.add_argument("--max-workers", type=int, default=8)
        return run_logs_fix_job(run_parser.parse_args())

    if len(sys.argv) > 1 and sys.argv[1] == "_run-s3-fix-job":
        run_parser = NoColorArgumentParser(prog="aws _run-s3-fix-job")
        run_parser.add_argument("command")
        run_parser.add_argument("job_id")
        run_parser.add_argument("--region")
        run_parser.add_argument("--log-bucket")
        run_parser.add_argument("--tag", action="append", default=[])
        run_parser.add_argument("--max-workers", type=int, default=8)
        return run_s3_fix_job(run_parser.parse_args())

    if any(arg in {"help", "--help", "-h"} for arg in sys.argv[1:]):
        code = _show_help(sys.argv[1:])
        if code is not None:
            return code

    parser = NoColorArgumentParser(prog="aws", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=NoColorArgumentParser)
    subparsers.add_parser("hello", help="Print a friendly greeting.", add_help=False)

    cloudwatch_parser = subparsers.add_parser("cloudwatch", help="CloudWatch utility commands.", add_help=False)
    cloudwatch_subparsers = cloudwatch_parser.add_subparsers(
        dest="cloudwatch_command",
        required=True,
        parser_class=NoColorArgumentParser,
    )
    dashboard_parser = cloudwatch_subparsers.add_parser(
        "create-dashboard",
        help="Create CloudWatch dashboards.",
        add_help=False,
    )
    dashboard_parser.add_argument("--dashboard", choices=("full", "simple", "all"), default="all")
    dashboard_parser.add_argument("--name", help="Dashboard name. With --dashboard all, the dashboard type is appended.")
    dashboard_parser.add_argument("--base-url", default=DEFAULT_ASSET_BASE_URL, help="Base URL for dashboard JSON assets.")
    dashboard_parser.add_argument("--max-workers", type=int, default=4, help="Maximum parallel dashboard operations.")

    inspect_parser = subparsers.add_parser("inspect", help="Run AWS best-practice inspections.", add_help=False)
    inspect_subparsers = inspect_parser.add_subparsers(
        dest="inspect_command",
        required=True,
        parser_class=NoColorArgumentParser,
    )

    create_parser = inspect_subparsers.add_parser(
        "create-inspect-job",
        help="Install the inspection binary and run a job in the background.",
        add_help=False,
    )
    create_parser.add_argument("--ids", help="Comma-separated check IDs to run.")
    create_parser.add_argument("--services", help="Comma-separated AWS services to inspect.")
    create_parser.add_argument("--concurrency", type=int, help="Maximum concurrent checks.")
    create_parser.add_argument("--no-prefetch", action="store_true", help="Disable inspection cache prefetching.")
    create_parser.add_argument("--force-install", action="store_true", help="Download the inspection binary even if it already exists.")

    describe_parser = inspect_subparsers.add_parser(
        "describe-inspect-job",
        help="Describe an inspection job as JSON.",
        add_help=False,
    )
    describe_parser.add_argument("--job-id", help="Inspection job ID returned by create-inspect-job. Omit to list all jobs.")

    logs_parser = subparsers.add_parser("logs", help="CloudWatch Logs utility commands.", add_help=False)
    logs_subparsers = logs_parser.add_subparsers(
        dest="logs_command",
        required=True,
        parser_class=NoColorArgumentParser,
    )
    logs_create_parser = logs_subparsers.add_parser(
        "create-fix-job",
        help="Create a background CloudWatch Logs fix job.",
        add_help=False,
    )
    logs_create_parser.add_argument("--region", help="AWS region. Defaults to AWS CLI configuration/environment.")
    logs_create_parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="Log retention in days. Defaults to 7.")
    logs_create_parser.add_argument("--tag", action="append", default=[], help="Tag to apply as Key=Value. May be repeated.")
    logs_create_parser.add_argument("--max-workers", type=int, default=8, help="Maximum parallel log group operations.")
    logs_describe_parser = logs_subparsers.add_parser(
        "describe-fix-job",
        help="Describe CloudWatch Logs fix jobs as JSON.",
        add_help=False,
    )
    logs_describe_parser.add_argument("--job-id", help="Logs fix job ID returned by create-fix-job. Omit to list all jobs.")

    s3_parser = subparsers.add_parser("s3", help="S3 utility commands.", add_help=False)
    s3_subparsers = s3_parser.add_subparsers(
        dest="s3_command",
        required=True,
        parser_class=NoColorArgumentParser,
    )
    s3_log_bucket_parser = s3_subparsers.add_parser(
        "create-log-bucket",
        help="Create or update a shared S3 log bucket.",
        add_help=False,
    )
    s3_log_bucket_parser.add_argument("--bucket", help="Log bucket name. Defaults to logbucket-<account-id>.")
    s3_log_bucket_parser.add_argument("--region", help="AWS region. Defaults to AWS CLI configuration/environment.")
    s3_create_parser = s3_subparsers.add_parser(
        "create-fix-job",
        help="Create a background S3 bucket fix job.",
        add_help=False,
    )
    s3_create_parser.add_argument("--region", help="AWS region for creating the log bucket. Defaults to AWS CLI configuration/environment.")
    s3_create_parser.add_argument("--log-bucket", help="Log bucket name. Defaults to logbucket-<account-id>.")
    s3_create_parser.add_argument("--tag", action="append", default=[], help="Tag to apply as Key=Value. May be repeated.")
    s3_create_parser.add_argument("--max-workers", type=int, default=8, help="Maximum parallel bucket operations.")
    s3_describe_parser = s3_subparsers.add_parser(
        "describe-fix-job",
        help="Describe S3 fix jobs as JSON.",
        add_help=False,
    )
    s3_describe_parser.add_argument("--job-id", help="S3 fix job ID returned by create-fix-job. Omit to list all jobs.")

    vpc_parser = subparsers.add_parser("vpc", help="VPC utility commands.", add_help=False)
    vpc_subparsers = vpc_parser.add_subparsers(
        dest="vpc_command",
        required=True,
        parser_class=NoColorArgumentParser,
    )
    vpc_create_parser = vpc_subparsers.add_parser(
        "create-fix-job",
        help="Create a background VPC endpoint and flow-log fix job.",
        add_help=False,
    )
    vpc_create_parser.add_argument("--vpc-ids", help="Comma-separated VPC IDs. Defaults to every VPC in the region.")
    vpc_create_parser.add_argument("--region", help="AWS region. Defaults to AWS CLI configuration/environment.")
    vpc_create_parser.add_argument("--max-workers", type=int, default=8, help="Maximum parallel VPC and endpoint operations.")
    vpc_describe_parser = vpc_subparsers.add_parser(
        "describe-fix-job",
        help="Describe VPC fix jobs as JSON.",
        add_help=False,
    )
    vpc_describe_parser.add_argument("--job-id", help="VPC fix job ID returned by create-fix-job. Omit to list all jobs.")

    args = parser.parse_args()
    if args.command == "hello":
        print("Hello!")
        return 0
    if args.command == "cloudwatch" and args.cloudwatch_command == "create-dashboard":
        return create_cloudwatch_dashboard(args)
    if args.command == "inspect" and args.inspect_command == "create-inspect-job":
        return _create_inspect_job(args)
    if args.command == "inspect" and args.inspect_command == "describe-inspect-job":
        return _describe_inspect_job(args)
    if args.command == "logs" and args.logs_command == "create-fix-job":
        return create_logs_fix_job(args)
    if args.command == "logs" and args.logs_command == "describe-fix-job":
        return describe_logs_fix_job(args)
    if args.command == "s3" and args.s3_command == "create-log-bucket":
        return create_log_bucket(args)
    if args.command == "s3" and args.s3_command == "create-fix-job":
        return create_s3_fix_job(args)
    if args.command == "s3" and args.s3_command == "describe-fix-job":
        return describe_s3_fix_job(args)
    if args.command == "vpc" and args.vpc_command == "create-fix-job":
        return create_vpc_fix_job(args)
    if args.command == "vpc" and args.vpc_command == "describe-fix-job":
        return describe_vpc_fix_job(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
