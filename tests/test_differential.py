from __future__ import annotations

import unittest

from mcp_system.differential import CapturedResponse, _encode_multipart, compare_runs, run_dual_scenario, run_scenario


class FakeTarget:
    def __init__(self, responses: list[CapturedResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[object, ...]] = []

    def request(self, method: str, path: str, *, query: object, body: object) -> CapturedResponse:
        self.requests.append((method, path, query, body))
        return self.responses.pop(0)


class FakeOperationTarget:
    def __init__(self, responses: list[CapturedResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, object]] = []

    def execute(self, operation: str, arguments: object) -> CapturedResponse:
        self.calls.append((operation, arguments))
        return self.responses.pop(0)


class DifferentialScenarioTests(unittest.TestCase):
    def test_dual_scenario_keeps_real_and_replica_identifiers_separate(self) -> None:
        scenario = {
            "steps": [
                {
                    "id": "create",
                    "real": {"method": "POST", "path": "/issues", "json": {"title": "Race"}},
                    "replica": {"operation": "create_issue", "arguments": {"title": "Race"}},
                    "expect": {"real_status": 201},
                    "capture": {"real": {"issue": "$.data.id"}, "replica": {"issue": "$.id"}},
                    "compare": {"real_body": "$.data", "semantic_paths": ["$.title"]},
                },
                {
                    "id": "read",
                    "real": {"method": "GET", "path": "/issues/${issue}"},
                    "replica": {"operation": "get_issue", "arguments": {"id": "${issue}"}},
                },
            ]
        }
        real_target = FakeTarget([
            CapturedResponse(201, {}, {"data": {"id": "real-9", "title": "Race"}}),
            CapturedResponse(200, {}, {"id": "real-9"}),
        ])
        local_target = FakeOperationTarget([
            CapturedResponse(200, {}, {"id": "local-1", "title": "Race"}),
            CapturedResponse(200, {}, {"id": "local-1"}),
        ])
        real, replica = run_dual_scenario(scenario, real_target, local_target)
        self.assertTrue(compare_runs(real, replica)["passed"])
        self.assertEqual(real_target.requests[1][1], "/issues/real-9")
        self.assertEqual(local_target.calls[1][1], {"id": "local-1"})

    def test_variables_capture_and_semantic_comparison(self) -> None:
        scenario = {
            "variables": {"project": "acme/product"},
            "steps": [
                {
                    "id": "create",
                    "request": {"method": "POST", "path": "/projects/${project}/issues", "json": {"title": "Issue ${run_id}"}},
                    "expect": {"status": 201},
                    "capture": {"issue_iid": "$.iid"},
                    "compare": {"semantic_paths": ["$.title", "$.state"], "ignore_shape_fields": ["web_url"]},
                },
                {"id": "read", "request": {"method": "GET", "path": "/issues/${issue_iid}"}, "expect": {"status": 200}},
            ],
        }
        real = run_scenario(scenario, FakeTarget([
            CapturedResponse(201, {}, {"id": 91, "iid": 7, "title": "Issue abc", "state": "opened", "web_url": "real", "extra": True}),
            CapturedResponse(200, {}, {"iid": 7}),
        ]), variables={"run_id": "abc"})
        replica_target = FakeTarget([
            CapturedResponse(201, {}, {"id": 1, "iid": 1, "title": "Issue abc", "state": "opened", "web_url": "replica"}),
            CapturedResponse(200, {}, {"iid": 1}),
        ])
        replica = run_scenario(scenario, replica_target, variables={"run_id": "abc"})
        report = compare_runs(real, replica)
        self.assertTrue(report["passed"], report)
        self.assertTrue(report["warnings"])
        self.assertEqual(replica_target.requests[1][1], "/issues/1")

    def test_invented_replica_field_fails(self) -> None:
        real = [{"id": "x", "response": {"status": 200, "headers": {}, "body": {"id": 1}}, "compare": {}}]
        replica = [{"id": "x", "response": {"status": 200, "headers": {}, "body": {"id": 2, "invented": True}}, "compare": {}}]
        report = compare_runs(real, replica)
        self.assertFalse(report["passed"])
        self.assertIn("invented", report["errors"][0])

    def test_shape_comparison_can_be_disabled_while_semantics_remain_checked(self) -> None:
        real = [{"id":"x","response":{"status":200,"headers":{},"body":{"summary":"same"}},"compare":{"shape":False,"semantic_paths":["$.summary"]}}]
        replica = [{"id":"x","response":{"status":200,"headers":{},"body":{"summary":"same","expanded":True}},"compare":{"shape":False,"semantic_paths":["$.summary"]}}]
        self.assertTrue(compare_runs(real, replica)["passed"])

    def test_multipart_encoder_preserves_fields_and_repository_paths(self) -> None:
        payload, content_type = _encode_multipart({"fields":{"branch":"feature"},"files":{"src/app.py":"print(1)\n"}})
        self.assertIn("boundary=", content_type)
        self.assertIn(b'name="branch"', payload)
        self.assertIn(b'name="src/app.py"; filename="app.py"', payload)
        self.assertIn(b"print(1)\n", payload)

    def test_dual_scenario_can_ignore_cleanup_response_bodies(self) -> None:
        scenario = {"steps": [{
            "id": "cleanup",
            "real": {"method": "DELETE", "path": "/items/1"},
            "replica": {"operation": "delete_item", "arguments": {"id": 1}},
            "expect": {"real_status": 204},
            "compare": {"body": False},
        }]}
        real_target = FakeTarget([CapturedResponse(204, {}, None)])
        local_target = FakeOperationTarget([CapturedResponse(200, {}, {"deleted": True})])
        real, replica = run_dual_scenario(scenario, real_target, local_target)
        self.assertTrue(compare_runs(real, replica)["passed"])

    def test_dual_scenario_polls_real_capture_without_repeating_local_call(self) -> None:
        scenario = {"steps": [{
            "id": "eventual",
            "real": {"method": "GET", "path": "/runs"},
            "replica": {"operation": "create_run"},
            "poll": {"attempts": 2, "interval_seconds": 0},
            "capture": {"real": {"run": "$.runs.0.id"}, "replica": {"run": "$.id"}},
            "compare": {"body": False},
        }]}
        real_target = FakeTarget([CapturedResponse(200, {}, {"runs": []}), CapturedResponse(200, {}, {"runs": [{"id": 9}]})])
        local_target = FakeOperationTarget([CapturedResponse(200, {}, {"id": 1})])
        real, replica = run_dual_scenario(scenario, real_target, local_target)
        self.assertTrue(compare_runs(real, replica)["passed"])
        self.assertEqual(len(local_target.calls), 1)


if __name__ == "__main__":
    unittest.main()
