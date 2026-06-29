import argparse
import concurrent.futures
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.error import URLError
from pathlib import Path
from urllib.request import urlopen, urlretrieve


INSTALL_ROOT = Path(os.environ.get("AWSUTILS_INSTALL_DIR", Path.home() / ".awsutils"))
BPTOOLS_DIR = INSTALL_ROOT / "bin"
BPTOOLS_BINARY = BPTOOLS_DIR / ("bptools.exe" if os.name == "nt" else "bptools")
JOBS_DIR = INSTALL_ROOT / "inspect" / "jobs"
VPC_JOBS_DIR = INSTALL_ROOT / "vpc" / "jobs"
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
DEFAULT_ASSET_BASE_URL = "https://awsutils.github.io"
GATEWAY_ENDPOINTS = ("s3", "dynamodb")
INTERFACE_ENDPOINTS = ("ecr.dkr", "ecr.api", "ssm", "ssmmessages", "ec2messages", "sqs", "sns")
PRINT_LOCK = threading.Lock()
RETRY_ATTEMPTS = 5


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


def _show_help(argv):
    topic = [arg for arg in argv if arg not in {"help", "--help", "-h"}]
    if topic == []:
        print("""NAME
     utils - AWS utility commands

DESCRIPTION
     Utility commands for AWS CLI.

SYNOPSIS
     aws utils <command> [parameters]

AVAILABLE COMMANDS
     * hello

     * cloudwatch

     * inspect

     * vpc
""")
        return 0
    if topic == ["cloudwatch"]:
        print("""NAME
     cloudwatch - CloudWatch utility commands

DESCRIPTION
     Create AWSUtils CloudWatch dashboards.

SYNOPSIS
     aws utils cloudwatch <command> [parameters]

AVAILABLE COMMANDS
     * create-dashboard
""")
        return 0
    if topic == ["cloudwatch", "create-dashboard"]:
        print("""NAME
     create-dashboard - Create CloudWatch dashboards

SYNOPSIS
     aws utils cloudwatch create-dashboard
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
     Installs and runs bptools inspection jobs in the background.

SYNOPSIS
     aws utils inspect <command> [parameters]

AVAILABLE COMMANDS
     * create-inspect-job

     * describe-inspect-job
""")
        return 0
    if topic == ["inspect", "create-inspect-job"]:
        print("""NAME
     create-inspect-job - Create an inspect job

DESCRIPTION
     Installs the bptools binary when needed and starts an AWS best-practice
     inspection job in the background.

SYNOPSIS
     aws utils inspect create-inspect-job
          [--ids <value>]
          [--services <value>]
          [--concurrency <value>]
          [--no-prefetch]
          [--force-install]

OPTIONS
     --ids (string)
          Comma-separated bptools check IDs to run.

     --services (string)
          Comma-separated AWS services to inspect.

     --concurrency (integer)
          Maximum concurrent bptools checks.

     --no-prefetch (boolean)
          Disable bptools cache prefetching.

     --force-install (boolean)
          Download the bptools binary even if it already exists.
""")
        return 0
    if topic == ["inspect", "describe-inspect-job"]:
        print("""NAME
     describe-inspect-job - Describe inspect jobs

DESCRIPTION
     Returns inspect job state as JSON. When --job-id is omitted, all known jobs
     are listed.

SYNOPSIS
     aws utils inspect describe-inspect-job
          [--job-id <value>]

OPTIONS
     --job-id (string)
          Inspection job ID returned by create-inspect-job. Omit to list all jobs.
""")
        return 0
    if topic == ["vpc"]:
        print("""NAME
     vpc - VPC utility commands

DESCRIPTION
     Start and describe background VPC fix jobs.

SYNOPSIS
     aws utils vpc <command> [parameters]

AVAILABLE COMMANDS
     * create-fix-job

     * describe-fix-job
""")
        return 0
    if topic == ["vpc", "create-fix-job"]:
        print("""NAME
     create-fix-job - Create a VPC fix job

DESCRIPTION
     Starts a background job that enables common VPC endpoints and S3 VPC Flow
     Logs for selected VPCs, or all VPCs in the current region.

SYNOPSIS
     aws utils vpc create-fix-job
          [--vpc-ids <value>]
          [--region <value>]
          [--max-workers <value>]
""")
        return 0
    if topic == ["vpc", "describe-fix-job"]:
        print("""NAME
     describe-fix-job - Describe VPC fix jobs

SYNOPSIS
     aws utils vpc describe-fix-job
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


def _run_command(cmd, *, check=False, retries=1):
    last_proc = None
    for attempt in range(1, retries + 1):
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        last_proc = proc
        if proc.returncode == 0:
            return proc
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    if check and last_proc and last_proc.returncode != 0:
        raise RuntimeError(last_proc.stderr.strip() or f"command failed: {' '.join(cmd)}")
    return last_proc


def _aws_base_cmd(args):
    cmd = ["aws", *args]
    return cmd


def _aws_json(args, default=None):
    proc = _run_command(_aws_base_cmd([*args, "--output", "json"]), retries=RETRY_ATTEMPTS)
    if proc.returncode != 0:
        return default
    text = proc.stdout.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _aws_text(args, default=""):
    proc = _run_command(_aws_base_cmd([*args, "--output", "text"]), retries=RETRY_ATTEMPTS)
    if proc.returncode != 0:
        return default
    text = proc.stdout.strip()
    if text == "None":
        return default
    return text


def _aws_ok(args):
    return _run_command(_aws_base_cmd(args), retries=RETRY_ATTEMPTS).returncode == 0


def _aws_call(args):
    proc = _run_command(_aws_base_cmd(args), retries=RETRY_ATTEMPTS)
    return {"ok": proc.returncode == 0, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def _parallel_map(items, worker, max_workers):
    items = list(items)
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    if workers == 1:
        return [worker(item) for item in items]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, item) for item in items]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return results


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
    return f"https://awsutils.github.io/bptools/bptools-{os_name}-{arch}{suffix}"


def _ensure_bptools(force=False):
    if BPTOOLS_BINARY.exists() and not force:
        return str(BPTOOLS_BINARY)

    BPTOOLS_DIR.mkdir(parents=True, exist_ok=True)
    url = os.environ.get("AWSUTILS_BPTOOLS_URL", _bptools_url())
    urlretrieve(url, BPTOOLS_BINARY)
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
    _json_dump(state)
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
    return state


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


def _download_text(url):
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urlopen(url, timeout=30) as response:
                return response.read().decode("utf-8")
        except URLError as exc:
            last_error = exc
            if attempt < RETRY_ATTEMPTS:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"could not download {url}: {last_error}")


def _create_cloudwatch_dashboard(args):
    base_url = args.base_url.rstrip("/")
    selected = ["full", "simple"] if args.dashboard == "all" else [args.dashboard]

    def create_one(dashboard):
        file_name = f"dashboard_{dashboard}.json"
        dashboard_name = args.name or f"dashboard-{dashboard}"
        if args.name and len(selected) > 1:
            dashboard_name = f"{args.name}-{dashboard}"

        body = _download_text(f"{base_url}/{file_name}")
        result = _aws_call([
            "cloudwatch",
            "put-dashboard",
            "--dashboard-name",
            dashboard_name,
            "--dashboard-body",
            body,
        ])
        return {
            "dashboard": dashboard,
            "dashboard_name": dashboard_name,
            "source": f"{base_url}/{file_name}",
            **result,
        }

    results = _parallel_map(selected, create_one, args.max_workers)

    _json_dump({"dashboards": results})
    return 0 if all(item["ok"] for item in results) else 1


def _vpc_job_paths(job_id):
    job_dir = VPC_JOBS_DIR / job_id
    return {
        "dir": job_dir,
        "state": job_dir / "job.json",
        "stdout": job_dir / "stdout.log",
        "stderr": job_dir / "stderr.log",
    }


def _create_vpc_fix_job(args):
    job_id = str(uuid.uuid4())
    paths = _vpc_job_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    runner_args = []
    if args.vpc_ids:
        runner_args.extend(["--vpc-ids", args.vpc_ids])
    if args.region:
        runner_args.extend(["--region", args.region])
    runner_args.extend(["--max-workers", str(args.max_workers)])

    state = {
        "job_id": job_id,
        "status": "PENDING",
        "created_at": _utc_now(),
        "stdout": str(paths["stdout"]),
        "stderr": str(paths["stderr"]),
        "vpc_ids": args.vpc_ids,
        "region": args.region,
        "max_workers": args.max_workers,
    }
    _write_json(paths["state"], state)

    cmd = [sys.executable, "-m", "awsutils.cli", "_run-vpc-fix-job", job_id, *runner_args]
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
    _json_dump(state)
    return 0


def _describe_vpc_fix_job(args):
    if not args.job_id:
        jobs = []
        if VPC_JOBS_DIR.exists():
            for state_path in sorted(VPC_JOBS_DIR.glob("*/job.json")):
                state = _read_json(state_path)
                state["stdout_bytes"] = len(_read_clean_text(state_path.parent / "stdout.log").encode("utf-8"))
                state["stderr_bytes"] = len(_read_clean_text(state_path.parent / "stderr.log").encode("utf-8"))
                jobs.append(state)
        _json_dump({"jobs": jobs})
        return 0

    paths = _vpc_job_paths(args.job_id)
    if not paths["state"].exists():
        _json_dump({"job_id": args.job_id, "status": "NOT_FOUND"})
        return 1
    state = _read_json(paths["state"])
    state["stdout_text"] = _read_clean_text(paths["stdout"])
    state["stderr_text"] = _read_clean_text(paths["stderr"])
    _json_dump(state)
    return 0


def _print_job_event(message):
    with PRINT_LOCK:
        print(f"[{_utc_now()}] {message}", flush=True)


def _region_arg(region):
    return ["--region", region] if region else []


def _detect_region(region):
    if region:
        return region
    detected = _aws_text(["ec2", "describe-availability-zones", "--query", "AvailabilityZones[0].RegionName"])
    if detected:
        return detected
    detected = _aws_text(["configure", "get", "region"])
    return detected or os.environ.get("AWS_DEFAULT_REGION", "")


def _vpc_name(vpc_id, region):
    data = _aws_json([
        "ec2",
        "describe-vpcs",
        *_region_arg(region),
        "--vpc-ids",
        vpc_id,
        "--query",
        "Vpcs[0]",
    ], default={}) or {}
    for tag in data.get("Tags", []):
        if tag.get("Key") == "Name" and tag.get("Value"):
            return tag["Value"]
    return vpc_id


def _vpc_cidr(vpc_id, region):
    return _aws_text(["ec2", "describe-vpcs", *_region_arg(region), "--vpc-ids", vpc_id, "--query", "Vpcs[0].CidrBlock"])


def _all_vpc_ids(region):
    text = _aws_text(["ec2", "describe-vpcs", *_region_arg(region), "--query", "Vpcs[*].VpcId"])
    return text.split() if text else []


def _route_tables(vpc_id, region):
    return _aws_json([
        "ec2",
        "describe-route-tables",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "RouteTables",
    ], default=[]) or []


def _all_route_table_ids(vpc_id, region):
    return [table.get("RouteTableId") for table in _route_tables(vpc_id, region) if table.get("RouteTableId")]


def _route_table_has_igw(table):
    for route in table.get("Routes", []):
        if route.get("DestinationCidrBlock") == "0.0.0.0/0" and str(route.get("GatewayId", "")).startswith("igw-"):
            return True
    return False


def _private_subnet_ids(vpc_id, region):
    tables = _route_tables(vpc_id, region)
    main_table = next((table for table in tables if any(assoc.get("Main") for assoc in table.get("Associations", []))), None)
    explicit = {}
    for table in tables:
        for assoc in table.get("Associations", []):
            subnet_id = assoc.get("SubnetId")
            if subnet_id:
                explicit[subnet_id] = table
    subnets = _aws_json([
        "ec2",
        "describe-subnets",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "Subnets[*].SubnetId",
    ], default=[]) or []
    private = []
    for subnet_id in subnets:
        table = explicit.get(subnet_id, main_table)
        if table and not _route_table_has_igw(table):
            private.append(subnet_id)
    return private


def _existing_endpoint_services(vpc_id, region):
    services = _aws_json([
        "ec2",
        "describe-vpc-endpoints",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "VpcEndpoints[?State!='deleted'].ServiceName",
    ], default=[]) or []
    return set(services)


def _ensure_endpoint_sg(vpc_id, vpc_name, vpc_cidr, region):
    sg_name = f"{vpc_name}-vpce-sg"
    existing = _aws_text([
        "ec2",
        "describe-security-groups",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        f"Name=tag:Name,Values={sg_name}",
        "--query",
        "SecurityGroups[0].GroupId",
    ])
    if existing:
        return existing
    sg_id = _aws_text([
        "ec2",
        "create-security-group",
        *_region_arg(region),
        "--group-name",
        sg_name,
        "--description",
        "VPC Endpoints",
        "--vpc-id",
        vpc_id,
        "--tag-specifications",
        f"ResourceType=security-group,Tags=[{{Key=Name,Value={sg_name}}}]",
        "--query",
        "GroupId",
    ])
    if not sg_id:
        return ""
    _aws_ok(["ec2", "authorize-security-group-ingress", *_region_arg(region), "--group-id", sg_id, "--protocol", "-1", "--cidr", vpc_cidr])
    return sg_id


def _ensure_log_bucket(bucket, region):
    if _aws_ok(["s3api", "head-bucket", "--bucket", bucket]):
        return True
    args = ["s3api", "create-bucket", "--bucket", bucket]
    if region != "us-east-1":
        args.extend(["--create-bucket-configuration", f"LocationConstraint={region}"])
    if not _aws_ok(args):
        return False
    _aws_ok([
        "s3api",
        "put-public-access-block",
        "--bucket",
        bucket,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
    ])
    return True


def _setup_vpc(vpc_id, region, account_id, max_workers):
    vpc_name = _vpc_name(vpc_id, region)
    vpc_cidr = _vpc_cidr(vpc_id, region)
    if not vpc_cidr:
        _print_job_event(f"{vpc_id}: could not read CIDR; skipping")
        return

    _print_job_event(f"{vpc_id}: configuring endpoints and flow logs")
    route_table_ids = _all_route_table_ids(vpc_id, region)
    existing = _existing_endpoint_services(vpc_id, region)

    def create_gateway_endpoint(short):
        service = f"com.amazonaws.{region}.{short}"
        if service in existing:
            _print_job_event(f"{vpc_id}: gateway endpoint {short} already exists")
            return {"endpoint": short, "type": "gateway", "ok": True, "skipped": True}
        if not route_table_ids:
            _print_job_event(f"{vpc_id}: no route tables; skipping gateway endpoint {short}")
            return {"endpoint": short, "type": "gateway", "ok": False, "skipped": True}
        result = _aws_call([
            "ec2",
            "create-vpc-endpoint",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--service-name",
            service,
            "--vpc-endpoint-type",
            "Gateway",
            "--route-table-ids",
            *route_table_ids,
            "--tag-specifications",
            f"ResourceType=vpc-endpoint,Tags=[{{Key=Name,Value={vpc_name}-vpce-{short}}}]",
        ])
        _print_job_event(f"{vpc_id}: {'created' if result['ok'] else 'failed'} gateway endpoint {short}")
        return {"endpoint": short, "type": "gateway", **result}

    _parallel_map(GATEWAY_ENDPOINTS, create_gateway_endpoint, max_workers)

    private_subnets = _private_subnet_ids(vpc_id, region)
    sg_id = _ensure_endpoint_sg(vpc_id, vpc_name, vpc_cidr, region) if private_subnets else ""

    def create_interface_endpoint(short):
        service = f"com.amazonaws.{region}.{short}"
        if service in existing:
            _print_job_event(f"{vpc_id}: interface endpoint {short} already exists")
            return {"endpoint": short, "type": "interface", "ok": True, "skipped": True}
        if not private_subnets or not sg_id:
            _print_job_event(f"{vpc_id}: skipping interface endpoint {short}; private subnets or security group unavailable")
            return {"endpoint": short, "type": "interface", "ok": False, "skipped": True}
        result = _aws_call([
            "ec2",
            "create-vpc-endpoint",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--service-name",
            service,
            "--vpc-endpoint-type",
            "Interface",
            "--subnet-ids",
            *private_subnets,
            "--security-group-ids",
            sg_id,
            "--private-dns-enabled",
            "--tag-specifications",
            f"ResourceType=vpc-endpoint,Tags=[{{Key=Name,Value={vpc_name}-vpce-{short}}}]",
        ])
        _print_job_event(f"{vpc_id}: {'created' if result['ok'] else 'failed'} interface endpoint {short}")
        return {"endpoint": short, "type": "interface", **result}

    _parallel_map(INTERFACE_ENDPOINTS, create_interface_endpoint, max_workers)

    existing_flowlog = _aws_text([
        "ec2",
        "describe-flow-logs",
        *_region_arg(region),
        "--filter",
        f"Name=resource-id,Values={vpc_id}",
        "Name=log-destination-type,Values=s3",
        "--query",
        "FlowLogs[0].FlowLogId",
    ])
    if existing_flowlog:
        _print_job_event(f"{vpc_id}: S3 flow logs already enabled")
        return
    bucket = f"logbucket-{account_id}"
    if not _ensure_log_bucket(bucket, region):
        _print_job_event(f"{vpc_id}: could not ensure flow-log bucket {bucket}")
        return
    result = _aws_call([
        "ec2",
        "create-flow-logs",
        *_region_arg(region),
        "--resource-ids",
        vpc_id,
        "--resource-type",
        "VPC",
        "--traffic-type",
        "ALL",
        "--log-destination-type",
        "s3",
        "--log-destination",
        f"arn:aws:s3:::{bucket}",
    ])
    _print_job_event(f"{vpc_id}: {'enabled' if result['ok'] else 'failed to enable'} S3 flow logs")


def _run_vpc_fix_job(args):
    paths = _vpc_job_paths(args.job_id)
    state = _read_json(paths["state"])
    state["status"] = "RUNNING"
    state["runner_pid"] = os.getpid()
    state.setdefault("started_at", _utc_now())
    _write_json(paths["state"], state)

    with paths["stdout"].open("w", encoding="utf-8") as stdout, paths["stderr"].open("w", encoding="utf-8") as stderr:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            region = _detect_region(args.region)
            if not region:
                raise RuntimeError("could not determine AWS region")
            account_id = _aws_text(["sts", "get-caller-identity", "--query", "Account"])
            if not account_id:
                raise RuntimeError("could not determine AWS account ID")
            vpc_ids = [item.strip() for item in (args.vpc_ids.split(",") if args.vpc_ids else _all_vpc_ids(region)) if item.strip()]
            _parallel_map(vpc_ids, lambda vpc_id: _setup_vpc(vpc_id, region, account_id, args.max_workers), args.max_workers)
            state["status"] = "SUCCEEDED"
            state["completed_at"] = _utc_now()
            state["region"] = region
            state["vpc_ids"] = ",".join(vpc_ids)
            _write_json(paths["state"], state)
            return 0
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            state["status"] = "FAILED"
            state["completed_at"] = _utc_now()
            state["error"] = str(exc)
            _write_json(paths["state"], state)
            return 1
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_run-inspect-job":
        run_parser = NoColorArgumentParser(prog="aws utils _run-inspect-job")
        run_parser.add_argument("command")
        run_parser.add_argument("job_id")
        run_parser.add_argument("binary")
        run_parser.add_argument("bptools_args", nargs=argparse.REMAINDER)
        return _run_inspect_job(run_parser.parse_args())

    if len(sys.argv) > 1 and sys.argv[1] == "_run-vpc-fix-job":
        run_parser = NoColorArgumentParser(prog="aws utils _run-vpc-fix-job")
        run_parser.add_argument("command")
        run_parser.add_argument("job_id")
        run_parser.add_argument("--vpc-ids")
        run_parser.add_argument("--region")
        run_parser.add_argument("--max-workers", type=int, default=8)
        return _run_vpc_fix_job(run_parser.parse_args())

    if any(arg in {"help", "--help", "-h"} for arg in sys.argv[1:]):
        code = _show_help(sys.argv[1:])
        if code is not None:
            return code

    parser = NoColorArgumentParser(prog="aws utils", add_help=False)
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
        help="Create AWSUtils CloudWatch dashboards.",
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
        help="Install bptools and run an inspection job in the background.",
        add_help=False,
    )
    create_parser.add_argument("--ids", help="Comma-separated bptools check IDs to run.")
    create_parser.add_argument("--services", help="Comma-separated AWS services to inspect.")
    create_parser.add_argument("--concurrency", type=int, help="Maximum concurrent bptools checks.")
    create_parser.add_argument("--no-prefetch", action="store_true", help="Disable bptools cache prefetching.")
    create_parser.add_argument("--force-install", action="store_true", help="Download the bptools binary even if it already exists.")

    describe_parser = inspect_subparsers.add_parser(
        "describe-inspect-job",
        help="Describe an inspection job as JSON.",
        add_help=False,
    )
    describe_parser.add_argument("--job-id", help="Inspection job ID returned by create-inspect-job. Omit to list all jobs.")

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
        print("Hello from aws utils!")
        return 0
    if args.command == "cloudwatch" and args.cloudwatch_command == "create-dashboard":
        return _create_cloudwatch_dashboard(args)
    if args.command == "inspect" and args.inspect_command == "create-inspect-job":
        return _create_inspect_job(args)
    if args.command == "inspect" and args.inspect_command == "describe-inspect-job":
        return _describe_inspect_job(args)
    if args.command == "vpc" and args.vpc_command == "create-fix-job":
        return _create_vpc_fix_job(args)
    if args.command == "vpc" and args.vpc_command == "describe-fix-job":
        return _describe_vpc_fix_job(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
