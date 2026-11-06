"""The agent loop, driven by a scripted fake of the Claude API (no network, no database)."""
from types import SimpleNamespace as NS

from spotify.agent import DataAgent, build_system_prompt
from spotify.tools import SqlRun, TOOL_SCHEMAS, load_metrics


def text(t):
    return NS(type="text", text=t)


def tool_use(id_, tool_name, **inp):
    return NS(type="tool_use", id=id_, name=tool_name, input=inp)


def reply(*content, stop="end_turn"):
    return NS(content=list(content), stop_reason=stop, model="fake-model", stop_details=None,
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=0))


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


class FakeTools:
    def __init__(self):
        self.metrics, self.runs, self.calls = load_metrics(), [], []

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "run_sql":
            if "DELETE" in args["sql"]:
                self.runs.append(SqlRun(args["sql"], args["purpose"], None, "read-only", 1))
                return "Only a SELECT is allowed. The data is read-only.", True
            self.runs.append(SqlRun(args["sql"], args["purpose"], 1, None, 1))
            return '{"columns": ["count"], "rows": [[89740]]}', False
        return "{}", False

    def close(self):
        pass


def make(replies, **kw):
    client, tools = FakeClient(replies), FakeTools()
    return DataAgent(client=client, tools=tools, log_path=None, **kw), client, tools


def test_runs_tools_and_returns_the_final_answer():
    agent, client, tools = make([
        reply(text("Let me count."), tool_use("t1", "get_metric", name="catalog_tracks"),
              tool_use("t2", "run_sql", sql="SELECT count(*) FROM gold.dim_track", purpose="count"), stop="tool_use"),
        reply(text("The catalog has 89,740 tracks.")),
    ])
    answer = agent.ask("How many tracks?")
    assert answer.text == "The catalog has 89,740 tracks."
    assert answer.stop_reason == "end_turn" and answer.steps == 2
    assert [r.sql for r in answer.sql_runs] == ["SELECT count(*) FROM gold.dim_track"]
    assert answer.usage == {"input_tokens": 200, "output_tokens": 40,
                            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}
    # Both tool results go back in ONE user message, matched to their tool_use ids.
    results = client.calls[1]["messages"][-1]
    assert results["role"] == "user"
    assert [r["tool_use_id"] for r in results["content"]] == ["t1", "t2"]
    assert all("is_error" not in r for r in results["content"])


def test_tool_errors_are_sent_back_flagged():
    agent, client, _ = make([
        reply(tool_use("t1", "run_sql", sql="DELETE FROM gold.dim_track", purpose="x"), stop="tool_use"),
        reply(text("I can't change data; it is read-only.")),
    ])
    agent.ask("Delete everything")
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "read-only" in result["content"]


def test_request_shape():
    agent, client, _ = make([reply(text("hi"))], model="claude-opus-5-5", effort="high")
    agent.ask("hello")
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_config"] == {"effort": "high"}
    assert call["tools"] is TOOL_SCHEMAS and call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in TOOL_SCHEMAS)


def test_history_is_kept_across_questions():
    agent, client, _ = make([reply(text("89,740.")), reply(text("Same as before."))])
    agent.ask("How many tracks?")
    agent.ask("And now?")
    roles = [m["role"] for m in client.calls[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_stops_after_max_steps():
    loop = [reply(tool_use(f"t{i}", "list_tables"), stop="tool_use") for i in range(3)]
    agent, _, _ = make(loop, max_steps=3)
    answer = agent.ask("Loop forever")
    assert answer.stop_reason == "max_steps" and answer.steps == 3 and "stopped after 3 steps" in answer.text


def test_refusal_is_reported():
    agent, _, _ = make([reply(stop="refusal")])
    answer = agent.ask("Something it declines")
    assert answer.stop_reason == "refusal" and answer.text.startswith("I can't help")


def test_system_prompt_lists_every_agreed_metric():
    metrics = load_metrics()
    prompt = build_system_prompt(metrics)
    assert all(f"- {name}:" in prompt for name in metrics)
