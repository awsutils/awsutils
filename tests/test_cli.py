import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from awsutils import cli


class InspectAffectedResourcesTests(unittest.TestCase):
    def test_no_findings_has_no_affected_resources(self):
        self.assertIsNone(cli._parse_stdout("No non-compliant resources found."))
        self.assertEqual(cli._affected_resources(None), [])

    def test_extracts_unique_failed_resources(self):
        result = {
            "rules": [
                {
                    "findings": [
                        {"status": "FAIL", "resource_id": "bucket-b"},
                        {"status": "FAIL", "resource_id": "bucket-a"},
                    ]
                },
                {
                    "findings": [
                        {"status": "FAIL", "resource_id": "bucket-a"},
                        {"status": "PASS", "resource_id": "bucket-c"},
                        {"status": "ERROR", "resource_id": "service-error"},
                    ]
                },
            ]
        }

        self.assertEqual(cli._affected_resources(result), ["bucket-a", "bucket-b"])

    def test_job_list_keeps_affected_resources(self):
        output = """rules_with_issues=1 findings=2

s3-bucket-public-read-prohibited (fail=2)
  description: S3 buckets should prohibit public read access
  docs: https://example.test/rule
  [FAIL] bucket-b — public read access is enabled
  [FAIL] bucket-a — public read access is enabled
"""
        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp)
            job_dir = jobs_dir / "job-123"
            job_dir.mkdir()
            (job_dir / "job.json").write_text(
                json.dumps({"job_id": "job-123", "status": "SUCCEEDED"}),
                encoding="utf-8",
            )
            (job_dir / "stdout.log").write_text(output, encoding="utf-8")
            (job_dir / "stderr.log").write_text("", encoding="utf-8")

            with mock.patch.object(cli, "JOBS_DIR", jobs_dir):
                details = cli._inspect_job_details("job-123", include_logs=False)

        self.assertEqual(details["affected_resources"], ["bucket-a", "bucket-b"])
        self.assertEqual(
            details["best_practice_result"]["rules_with_issues"][0]["affected_resources"],
            ["bucket-a", "bucket-b"],
        )


if __name__ == "__main__":
    unittest.main()
