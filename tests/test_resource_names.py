import json
import sys
import unittest
from unittest import mock

from awsutils import backup, cli, s3


class ResourceNameTests(unittest.TestCase):
    def test_backup_cli_has_no_default_project_prefix(self):
        captured = []
        with (
            mock.patch.object(sys, "argv", ["aws", "backup", "create-backup"]),
            mock.patch.object(cli, "create_backup_job", side_effect=lambda args: captured.append(args) or 0),
        ):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(captured[0].prefix, "")

    def test_backup_name_has_no_default_project_prefix(self):
        with mock.patch.object(backup, "_timestamp", return_value="20260801120000"):
            name = backup._safe_name("", "rds", "database-1")

        self.assertEqual(name, "b-20260801120000-rds-database-1")

    def test_s3_configuration_ids_are_purpose_based(self):
        identifiers = [
            s3.INTELLIGENT_TIERING_ID,
            s3.LIFECYCLE_RULE_ID,
            *(statement["Sid"] for statement in s3._log_bucket_policy_statements("logs-123", "123")),
            s3._deny_insecure_transport_statement("bucket-123")["Sid"],
        ]

        self.assertFalse(any("awsutils" in identifier.casefold() for identifier in identifiers))

    def test_equivalent_policy_statement_is_replaced_by_new_identifier(self):
        desired = s3._deny_insecure_transport_statement("bucket-123")
        existing = {**desired, "Sid": "PreviousManagedIdentifier"}

        with (
            mock.patch.object(
                s3,
                "_existing_bucket_policy",
                return_value={"Version": "2012-10-17", "Statement": [existing]},
            ),
            mock.patch.object(s3, "_aws_ok", return_value=True) as aws_ok,
        ):
            self.assertTrue(s3._put_merged_bucket_policy("bucket-123", [desired]))

        args = aws_ok.call_args.args[0]
        policy = json.loads(args[args.index("--policy") + 1])
        self.assertEqual(policy["Statement"], [desired])


if __name__ == "__main__":
    unittest.main()
