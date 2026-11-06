"""The Spotify data agent: Claude in a loop with four read-only tools over the gold schema.

    python -m spotify.agent "Do explicit tracks get higher popularity?"
    python -m spotify.agent --show-sql "How many tracks do we have?"
    python -m spotify.agent                      # chat; 'exit' to quit

Every question, the SQL it ran and the answer are appended to data/agent_log.jsonl.
Settings (all optional): AGENT_MODEL, AGENT_EFFORT, AGENT_MAX_STEPS, AGENT_LOG,
AGENT_STATEMENT_TIMEOUT. The Claude API key comes from ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from .common import REPO, env
from .tools import TOOL_SCHEMAS, GoldTools, SqlRun

MODEL = env("AGENT_MODEL", "claude-opus-5-5")
EFFORT = env("AGENT_EFFORT", "high")
MAX_STEPS = int(env("AGENT_MAX_STEPS", "15"))
LOG_PATH = Path(env("AGENT_LOG", str(REPO / "data" / "agent_log.jsonl")))
# If the model declines a request, the API retries it on a fallback model it picks.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_TEMPLATE = """\
You are the data agent for a Spotify track catalog. People from the Label, Editorial and Product teams ask you business questions, and you answer them from the data using your tools.

## The data
- You can read only the gold schema of a PostgreSQL database, through a read-only login. You cannot change data; if someone asks you to, say so plainly and point them to the data team.
- Gold is a star schema built from a Kaggle snapshot of about 114,000 Spotify track listings. Each table's comment states its grain (what one row is). Read it with list_tables before relying on a table.
- Popularity and audio features belong to a track (gold.dim_track). Genres and artists hang off tracks through bridge tables. Counting bridge rows counts pairs, not tracks, and a track listed under several genres appears once per genre.
- Genres come from a sample of about 1,000 listings per genre, so genre sizes reflect the sample design, not the market.
- Popularity is Spotify's 0-100 score at snapshot time. There are no streams, revenue, dates, countries or listener data. If a question needs those, say what is missing instead of substituting something else.

## Agreed metrics
These definitions are agreed across teams. When a question touches one, call get_metric and compute it exactly that way; don't invent an alternative.
{metric_index}

## How to answer
- Run the query before you state a number, and sanity-check row counts against the table's grain.
- Lead with the answer in one or two sentences. Then a small table if it helps, then only the caveats that matter.
- When you compare groups, say that the difference is a correlation, not a proven cause.
- End with the SQL you relied on in a ```sql block, so anyone can rerun it.
- Keep it short and in plain words."""


def build_system_prompt(metrics: dict) -> str:
    index = "\n".join(f"- {name}: {' '.join(spec['definition'].split())}" for name, spec in metrics.items())
    return SYSTEM_TEMPLATE.format(metric_index=index)


@dataclass
class Answer:
    question: str
    text: str
    stop_reason: str
    steps: int
    sql_runs: list[SqlRun] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)


def _add_usage(total: dict[str, int], usage: Any) -> None:
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        total[key] = total.get(key, 0) + (getattr(usage, key, 0) or 0)


class DataAgent:
    """One conversation. ask() can be called repeatedly; the history is kept."""

    def __init__(self, client: Any = None, tools: Any = None, model: str = MODEL, effort: str = EFFORT,
                 max_steps: int = MAX_STEPS, log_path: Path | None = LOG_PATH):
        self.client = client or anthropic.Anthropic()
        self.tools = tools or GoldTools()
        self.model, self.effort, self.max_steps, self.log_path = model, effort, max_steps, log_path
        # Tools + system prompt never change within a conversation, so they are cached once.
        self.system = [{"type": "text", "text": build_system_prompt(self.tools.metrics),
                        "cache_control": {"type": "ephemeral"}}]
        self.messages: list[dict] = []

    def ask(self, question: str) -> Answer:
        self.messages.append({"role": "user", "content": question})
        first_run = len(self.tools.runs)
        usage: dict[str, int] = {}
        models: list[str] = []
        text, stop_reason, step = "", "max_steps", 0

        for step in range(1, self.max_steps + 1):
            response = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=self.system, tools=TOOL_SCHEMAS,
                messages=self.messages, output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},  # also cache the growing conversation
                betas=[FALLBACK_BETA], fallbacks="default",
            )
            _add_usage(usage, response.usage)
            models.append(response.model)
            # Append the whole content (thinking and tool_use blocks included), unchanged.
            self.messages.append({"role": "assistant", "content": response.content})
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            stop_reason = response.stop_reason

            if stop_reason != "tool_use":
                break
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    content, is_error = self.tools.call(block.name, block.input)
                    result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                    if is_error:
                        result["is_error"] = True
                    results.append(result)
            self.messages.append({"role": "user", "content": results})  # all results in ONE message
        else:
            stop_reason = "max_steps"

        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            text = "I can't help with that request." + (f" ({details.explanation})" if details and details.explanation else "")
        elif stop_reason == "max_tokens":
            text += "\n\n(The answer was cut off at the length limit.)"
        elif stop_reason == "max_steps":
            text = f"I stopped after {self.max_steps} steps without a final answer. Try a narrower question."

        answer = Answer(question, text, stop_reason, step, self.tools.runs[first_run:], usage, models)
        self._log(answer)
        return answer

    def _log(self, answer: Answer) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {"ts": datetime.now(timezone.utc).isoformat(), "model": self.model, "effort": self.effort,
                      **asdict(answer)}
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as e:  # logging must never break an answer
            print(f"(could not write the agent log: {e})", file=sys.stderr)


def _print(answer: Answer, show_sql: bool) -> None:
    print(answer.text)
    if show_sql:
        for i, run in enumerate(answer.sql_runs, 1):
            status = f"error: {run.error}" if run.error else f"{run.row_count} rows"
            print(f"\n-- query {i}: {run.purpose} ({status}, {run.ms} ms)\n{run.sql}")
    u = answer.usage
    print(f"\n[{answer.steps} steps, {len(answer.sql_runs)} queries, "
          f"{u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0)} in / {u.get('output_tokens', 0)} out tokens]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ask the Spotify data agent a question.")
    ap.add_argument("question", nargs="*")
    ap.add_argument("--show-sql", action="store_true", help="print every query the agent ran")
    a = ap.parse_args()

    agent = DataAgent()
    try:
        if a.question:
            _print(agent.ask(" ".join(a.question)), a.show_sql)
            return
        print("Spotify data agent. Ask a question; 'exit' to quit.")
        while True:
            try:
                q = input("\nyou> ").strip()
            except EOFError:
                break
            if q.lower() in {"exit", "quit"}:
                break
            if q:
                _print(agent.ask(q), a.show_sql)
    except TypeError as e:  # the SDK raises this when no key is configured at all
        if "authentication" not in str(e):
            raise
        sys.exit("No Claude API key found. Set ANTHROPIC_API_KEY (see spotify/README.md).")
    except anthropic.AuthenticationError:
        sys.exit("The Claude API rejected the key. Set ANTHROPIC_API_KEY (see spotify/README.md).")
    except anthropic.RateLimitError:
        sys.exit("Rate limited by the Claude API even after retries. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        sys.exit(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Could not reach the Claude API. Check the network.")
    finally:
        agent.tools.close()


if __name__ == "__main__":
    main()
