import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


INSTALL_ROOT = Path(os.environ.get("AWSUTILS_INSTALL_DIR", Path.home() / ".aws" / "cli" / "tools"))
S3_JOBS_DIR = INSTALL_ROOT / "s3" / "jobs"
PRINT_LOCK = threading.Lock()
RETRY_ATTEMPTS = 5
DEFAULT_TAGS = {
    "Environment": "Production",
}
INTELLIGENT_TIERING_ID = "archive-tiering"
LIFECYCLE_RULE_ID = "archive-lifecycle"


def _intelligent_tiering_config(days_and_tiers, filter_prefix=None):
    config = {
        "Id": INTELLIGENT_TIERING_ID,
        "Status": "Enabled",
        "Tierings": days_and_tiers,
    }
    if filter_prefix is not None:
        config["Filter"] = {"Prefix": filter_prefix}
    return config


def _json_dump(data):
    print(json.dumps(data, indent=4, sort_keys=True))


def _public_job_state(state):
    return {key: value for key, value in state.items() if key not in {"stdout", "stderr"}}


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_clean_text(path):
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _run_command(cmd, *, retries=1):
    last_proc = None
    for attempt in range(1, retries + 1):
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        last_proc = proc
        if proc.returncode == 0:
            return proc
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    return last_proc


def _aws_json(args, default=None):
    proc = _run_command(["aws", *args, "--output", "json"], retries=RETRY_ATTEMPTS)
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
    proc = _run_command(["aws", *args, "--output", "text"], retries=RETRY_ATTEMPTS)
    if proc.returncode != 0:
        return default
    text = proc.stdout.strip()
    if text == "None":
        return default
    return text


def _aws_ok(args):
    return _run_command(["aws", *args], retries=RETRY_ATTEMPTS).returncode == 0


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


def _print_job_event(message):
    with PRINT_LOCK:
        print(f"[{_utc_now()}] {message}", flush=True)


def _region_arg(region):
    return ["--region", region] if region else []


def _detect_region(region):
    if region:
        return region
    proc = _run_command(["aws", "configure", "get", "region"], retries=RETRY_ATTEMPTS)
    detected = proc.stdout.strip() if proc.returncode == 0 else ""
    return detected or os.environ.get("AWS_DEFAULT_REGION", "")


def _account_id():
    return _aws_text(["sts", "get-caller-identity", "--query", "Account"])


def default_log_bucket_name(account_id):
    return f"logbucket-{account_id}"


def _bucket_arn(bucket):
    return f"arn:aws:s3:::{bucket}"


def _bucket_object_arn(bucket, prefix="*"):
    return f"arn:aws:s3:::{bucket}/{prefix}"


def _create_bucket(bucket, region):
    if _aws_ok(["s3api", "head-bucket", "--bucket", bucket]):
        return True
    args = ["s3api", "create-bucket", *_region_arg(region), "--bucket", bucket]
    if region != "us-east-1":
        args.extend(["--create-bucket-configuration", f"LocationConstraint={region}"])
    return _aws_ok(args)


def _existing_bucket_policy(bucket):
    data = _aws_json(["s3api", "get-bucket-policy", "--bucket", bucket], default={}) or {}
    policy = data.get("Policy")
    if not policy:
        return {"Version": "2012-10-17", "Statement": []}
    try:
        return json.loads(policy)
    except json.JSONDecodeError:
        return {"Version": "2012-10-17", "Statement": []}


def _put_merged_bucket_policy(bucket, statements):
    policy = _existing_bucket_policy(bucket)
    managed_sids = {item["Sid"] for item in statements}
    managed_bodies = [{key: value for key, value in item.items() if key != "Sid"} for item in statements]
    existing = []
    for statement in policy.get("Statement", []):
        body = {key: value for key, value in statement.items() if key != "Sid"}
        if statement.get("Sid") in managed_sids or body in managed_bodies:
            continue
        existing.append(statement)
    policy["Version"] = policy.get("Version", "2012-10-17")
    policy["Statement"] = [*existing, *statements]
    return _aws_ok(["s3api", "put-bucket-policy", "--bucket", bucket, "--policy", json.dumps(policy)])


def _log_bucket_policy_statements(bucket, account_id):
    return [
        {
            "Sid": "AllowVPCFlowLogsAclCheck",
            "Effect": "Allow",
            "Principal": {"Service": "delivery.logs.amazonaws.com"},
            "Action": ["s3:GetBucketAcl", "s3:ListBucket"],
            "Resource": _bucket_arn(bucket),
            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
        },
        {
            "Sid": "AllowVPCFlowLogsWrite",
            "Effect": "Allow",
            "Principal": {"Service": "delivery.logs.amazonaws.com"},
            "Action": "s3:PutObject",
            "Resource": _bucket_object_arn(bucket, f"AWSLogs/{account_id}/*"),
            "Condition": {
                "StringEquals": {
                    "aws:SourceAccount": account_id,
                    "s3:x-amz-acl": "bucket-owner-full-control",
                }
            },
        },
        {
            "Sid": "AllowALBLogDeliveryWrite",
            "Effect": "Allow",
            "Principal": {"Service": "logdelivery.elasticloadbalancing.amazonaws.com"},
            "Action": "s3:PutObject",
            "Resource": _bucket_object_arn(bucket, f"AWSLogs/{account_id}/*"),
        },
        {
            "Sid": "AllowS3ServerAccessLogsWrite",
            "Effect": "Allow",
            "Principal": {"Service": "logging.s3.amazonaws.com"},
            "Action": "s3:PutObject",
            "Resource": _bucket_object_arn(bucket, "s3-access-logs/*"),
            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
        },
    ]


def _deny_insecure_transport_statement(bucket):
    return {
        "Sid": "DenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [_bucket_arn(bucket), _bucket_object_arn(bucket)],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }


def ensure_log_bucket(bucket=None, region=None, account_id=None):
    region = _detect_region(region)
    account_id = account_id or _account_id()
    if not region:
        raise RuntimeError("could not determine AWS region")
    if not account_id:
        raise RuntimeError("could not determine AWS account ID")
    bucket = bucket or default_log_bucket_name(account_id)

    created_or_exists = _create_bucket(bucket, region)
    public_access_ok = _aws_ok([
        "s3api",
        "put-public-access-block",
        "--bucket",
        bucket,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
    ])
    encryption_ok = _aws_ok([
        "s3api",
        "put-bucket-encryption",
        "--bucket",
        bucket,
        "--server-side-encryption-configuration",
        json.dumps({"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}),
    ])
    versioning_ok = _aws_ok([
        "s3api",
        "put-bucket-versioning",
        "--bucket",
        bucket,
        "--versioning-configuration",
        "Status=Enabled",
    ])
    policy_ok = _put_merged_bucket_policy(bucket, _log_bucket_policy_statements(bucket, account_id))

    return {
        "bucket": bucket,
        "region": region,
        "account_id": account_id,
        "ok": created_or_exists and public_access_ok and encryption_ok and versioning_ok and policy_ok,
        "created_or_exists": created_or_exists,
        "public_access_block_ok": public_access_ok,
        "encryption_ok": encryption_ok,
        "versioning_ok": versioning_ok,
        "policy_ok": policy_ok,
    }


def _parse_tags(tag_args):
    tags = dict(DEFAULT_TAGS)
    for item in tag_args or []:
        if "=" not in item:
            raise ValueError(f"invalid tag '{item}'; expected Key=Value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid tag '{item}'; tag key is empty")
        tags[key] = value.strip()
    return tags


def _list_buckets():
    data = _aws_json(["s3api", "list-buckets"], default={}) or {}
    return [bucket.get("Name") for bucket in data.get("Buckets", []) if bucket.get("Name")]


def _bucket_region(bucket):
    region = _aws_text(["s3api", "get-bucket-location", "--bucket", bucket, "--query", "LocationConstraint"])
    return "us-east-1" if not region else region


def _put_bucket_tags(bucket, tags):
    existing = _aws_json(["s3api", "get-bucket-tagging", "--bucket", bucket], default={}) or {}
    merged = {item.get("Key"): item.get("Value", "") for item in existing.get("TagSet", []) if item.get("Key")}
    merged.update(tags)
    tag_set = [{"Key": key, "Value": value} for key, value in sorted(merged.items())]
    return _aws_ok(["s3api", "put-bucket-tagging", "--bucket", bucket, "--tagging", json.dumps({"TagSet": tag_set})])


def _put_intelligent_tiering(bucket):
    full_config = _intelligent_tiering_config([
        {"Days": 90, "AccessTier": "ARCHIVE_ACCESS"},
        {"Days": 180, "AccessTier": "DEEP_ARCHIVE_ACCESS"},
    ])
    archive_only_config = _intelligent_tiering_config([
        {"Days": 90, "AccessTier": "ARCHIVE_ACCESS"},
    ])

    def _put(config):
        proc = _run_command([
            "aws",
            "s3api",
            "put-bucket-intelligent-tiering-configuration",
            "--bucket",
            bucket,
            "--id",
            INTELLIGENT_TIERING_ID,
            "--intelligent-tiering-configuration",
            json.dumps(config),
        ], retries=RETRY_ATTEMPTS)
        if proc.returncode == 0:
            return True
        message = proc.stderr.strip()
        if message:
            _print_job_event(f"{bucket}: intelligent-tiering configuration update failed: {message}")
        return False

    if _put(full_config):
        return True

    _print_job_event(f"{bucket}: falling back to archive-only intelligent-tiering")
    return _put(archive_only_config)


def _put_lifecycle(bucket):
    config = {
        "Rules": [
            {
                "ID": LIFECYCLE_RULE_ID,
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 90},
            }
        ]
    }
    return _aws_ok(["s3api", "put-bucket-lifecycle-configuration", "--bucket", bucket, "--lifecycle-configuration", json.dumps(config)])


def _put_server_logging(bucket, log_bucket):
    config = {
        "LoggingEnabled": {
            "TargetBucket": log_bucket,
            "TargetPrefix": f"s3-access-logs/{bucket}/",
        }
    }
    return _aws_ok(["s3api", "put-bucket-logging", "--bucket", bucket, "--bucket-logging-status", json.dumps(config)])


def _fix_bucket(bucket, log_bucket, tags):
    versioning_ok = _aws_ok(["s3api", "put-bucket-versioning", "--bucket", bucket, "--versioning-configuration", "Status=Enabled"])
    tagging_ok = _put_bucket_tags(bucket, tags)
    tiering_ok = _put_intelligent_tiering(bucket)
    lifecycle_ok = _put_lifecycle(bucket)
    policy_ok = _put_merged_bucket_policy(bucket, [_deny_insecure_transport_statement(bucket)])
    logging_ok = _put_server_logging(bucket, log_bucket)
    ok = versioning_ok and tagging_ok and tiering_ok and lifecycle_ok and policy_ok and logging_ok
    _print_job_event(
        f"{bucket}: {'fixed' if ok else 'failed'} versioning={versioning_ok} tags={tagging_ok} "
        f"tiering={tiering_ok} lifecycle={lifecycle_ok} policy={policy_ok} logging={logging_ok}"
    )
    return {
        "bucket": bucket,
        "ok": ok,
        "versioning_ok": versioning_ok,
        "tagging_ok": tagging_ok,
        "intelligent_tiering_ok": tiering_ok,
        "lifecycle_ok": lifecycle_ok,
        "policy_ok": policy_ok,
        "server_logging_ok": logging_ok,
    }


def _s3_job_paths(job_id):
    job_dir = S3_JOBS_DIR / job_id
    return {
        "dir": job_dir,
        "state": job_dir / "job.json",
        "stdout": job_dir / "stdout.log",
        "stderr": job_dir / "stderr.log",
    }


def create_log_bucket(args):
    result = ensure_log_bucket(bucket=args.bucket, region=args.region)
    _json_dump(result)
    return 0 if result["ok"] else 1


def _create_s3_fix_job(args):
    tags = _parse_tags(args.tag)
    job_id = str(uuid.uuid4())
    paths = _s3_job_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    runner_args = ["--max-workers", str(args.max_workers)]
    if args.region:
        runner_args.extend(["--region", args.region])
    if args.log_bucket:
        runner_args.extend(["--log-bucket", args.log_bucket])
    for key, value in tags.items():
        runner_args.extend(["--tag", f"{key}={value}"])

    state = {
        "job_id": job_id,
        "status": "PENDING",
        "created_at": _utc_now(),
        "stdout": str(paths["stdout"]),
        "stderr": str(paths["stderr"]),
        "region": args.region,
        "log_bucket": args.log_bucket,
        "tags": tags,
        "max_workers": args.max_workers,
    }
    _write_json(paths["state"], state)

    cmd = [sys.executable, "-m", "awsutils.cli", "_run-s3-fix-job", job_id, *runner_args]
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


def _describe_s3_fix_job(args):
    if not args.job_id:
        jobs = []
        if S3_JOBS_DIR.exists():
            for state_path in sorted(S3_JOBS_DIR.glob("*/job.json")):
                state = _read_json(state_path)
                state["stdout_bytes"] = len(_read_clean_text(state_path.parent / "stdout.log").encode("utf-8"))
                state["stderr_bytes"] = len(_read_clean_text(state_path.parent / "stderr.log").encode("utf-8"))
                jobs.append(_public_job_state(state))
        _json_dump({"jobs": jobs})
        return 0

    paths = _s3_job_paths(args.job_id)
    if not paths["state"].exists():
        _json_dump({"job_id": args.job_id, "status": "NOT_FOUND"})
        return 1
    state = _read_json(paths["state"])
    state["stdout_text"] = _read_clean_text(paths["stdout"])
    state["stderr_text"] = _read_clean_text(paths["stderr"])
    _json_dump(_public_job_state(state))
    return 0


def _run_s3_fix_job(args):
    paths = _s3_job_paths(args.job_id)
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
            account_id = _account_id()
            if not region:
                raise RuntimeError("could not determine AWS region")
            if not account_id:
                raise RuntimeError("could not determine AWS account ID")
            log_bucket_result = ensure_log_bucket(bucket=args.log_bucket, region=region, account_id=account_id)
            if not log_bucket_result["ok"]:
                raise RuntimeError(f"could not ensure log bucket: {log_bucket_result['bucket']}")
            tags = _parse_tags(args.tag)
            buckets = _list_buckets()
            results = _parallel_map(buckets, lambda bucket: _fix_bucket(bucket, log_bucket_result["bucket"], tags), args.max_workers)
            failed = [result for result in results if not result.get("ok")]
            state["status"] = "SUCCEEDED" if not failed else "FAILED"
            state["completed_at"] = _utc_now()
            state["region"] = region
            state["log_bucket"] = log_bucket_result["bucket"]
            state["bucket_count"] = len(buckets)
            state["failed_count"] = len(failed)
            state["tags"] = tags
            _write_json(paths["state"], state)
            return 0 if not failed else 1
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            state["status"] = "FAILED"
            state["completed_at"] = _utc_now()
            state["error"] = str(exc)
            _write_json(paths["state"], state)
            return 1
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


create_s3_fix_job = _create_s3_fix_job
describe_s3_fix_job = _describe_s3_fix_job
run_s3_fix_job = _run_s3_fix_job
