import unittest
from contextlib import ExitStack
from unittest import mock

from awsutils import vpc


class RegionalNatGatewayTests(unittest.TestCase):
    def test_discovers_regional_nat_gateway_without_subnet_filter(self):
        with mock.patch.object(vpc, "_aws_text", return_value="nat-regional") as aws_text:
            self.assertEqual(vpc._regional_nat_gateway_id("vpc-123", "us-east-1"), "nat-regional")

        args = aws_text.call_args.args[0]
        self.assertIn("Name=vpc-id,Values=vpc-123", args)
        self.assertFalse(any(arg.startswith("Name=subnet-id,") for arg in args))
        self.assertIn("NatGateways[?AvailabilityMode=='regional'].NatGatewayId | [0]", args)

    def test_reuses_regional_nat_without_public_subnets(self):
        subnets = [
            {"SubnetId": "subnet-private-a", "AvailabilityZone": "us-east-1a", "CidrBlock": "10.0.1.0/24"},
            {"SubnetId": "subnet-private-b", "AvailabilityZone": "us-east-1b", "CidrBlock": "10.0.2.0/24"},
        ]

        def subnet_ids_by_route(_vpc_id, _region, public):
            return [] if public else ["subnet-private-a", "subnet-private-b"]

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(vpc, "_ensure_igw", return_value="igw-123"))
            stack.enter_context(mock.patch.object(vpc, "_availability_zones", return_value=["us-east-1a", "us-east-1b"]))
            stack.enter_context(mock.patch.object(vpc, "_regional_nat_gateway_id", return_value="nat-regional"))
            stack.enter_context(mock.patch.object(vpc, "_subnets", return_value=subnets))
            stack.enter_context(mock.patch.object(vpc, "_subnet_ids_by_route", side_effect=subnet_ids_by_route))
            stack.enter_context(mock.patch.object(vpc, "_parallel_map", side_effect=lambda items, worker, max_workers: [worker(item) for item in items]))
            stack.enter_context(mock.patch.object(vpc, "_aws_ok", return_value=True))
            create_subnet = stack.enter_context(mock.patch.object(vpc, "_create_subnet"))
            ensure_public_route_table = stack.enter_context(mock.patch.object(vpc, "_ensure_public_route_table"))
            ensure_zonal_nat = stack.enter_context(mock.patch.object(vpc, "_ensure_nat_gateway"))
            ensure_private_routes = stack.enter_context(mock.patch.object(vpc, "_ensure_private_routes"))
            stack.enter_context(mock.patch.object(vpc, "_print_job_event"))

            vpc._fix_vpc_networking("vpc-123", "example", "10.0.0.0/16", "us-east-1")

        create_subnet.assert_not_called()
        ensure_public_route_table.assert_not_called()
        ensure_zonal_nat.assert_not_called()
        self.assertEqual(ensure_private_routes.call_count, 2)
        self.assertEqual({call.args[2] for call in ensure_private_routes.call_args_list}, {"nat-regional"})


if __name__ == "__main__":
    unittest.main()
