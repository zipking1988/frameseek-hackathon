from frameseek.demo import catalog


def test_review_routes_have_valid_evidence_and_scoped_continuations():
    data = catalog()
    queries = {query["id"]: query for query in data["queries"]}
    assert len(queries) == 17
    assert len(data["scenarios"]) == 6
    assert len({route["id"] for route in data["scenarios"]}) == 6
    for query in queries.values():
        assert query["suggested_replies"] == list(query["followup_map"])
        assert all(target in queries for target in query["followup_map"].values())
        for event in query["response"]["events"]:
            assert 0 <= event["start"] < event["end"] <= data["video"]["duration"]
            assert all(event["start"] <= frame["t"] < event["end"] for frame in event["evidence"])
    for route in data["scenarios"]:
        assert len(route["steps"]) == 3
        for index, step in enumerate(route["steps"]):
            assert step["query_id"] in queries
            assert step["parent"] is None or 0 <= step["parent"] < index
            if step["parent"] is not None:
                parent_query = queries[route["steps"][step["parent"]]["query_id"]]
                question = step.get("question", queries[step["query_id"]]["question"])
                assert parent_query["followup_map"][question] == step["query_id"]
    boundary = next(route for route in data["scenarios"] if route["id"] == "boundaries")
    assert [queries[step["query_id"]]["response"]["status"] for step in boundary["steps"]] == [
        "no_result",
        "error",
        "ok",
    ]
