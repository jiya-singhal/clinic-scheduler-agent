import copy
from types import SimpleNamespace as NS

from agent.backend import Clinic
from agent.loop import ANTHROPIC_TOOLS, Agent, anthropic_model


class FakeAnthropic:
    """Replays scripted Anthropic responses and records every request."""
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []
        self.messages = self

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        return NS(content=next(self.responses))


def text(t):
    return NS(type="text", text=t)


def tool_use(name, input):
    return NS(type="tool_use", id="toolu_server_side", name=name, input=input)


def test_tool_schemas_map_nullable_to_json_schema():
    slots = next(t for t in ANTHROPIC_TOOLS if t["name"] == "get_available_slots")["input_schema"]
    assert slots["properties"]["provider_id"]["type"] == ["string", "null"]
    assert "nullable" not in str(slots)
    assert slots["required"] == ["date_from", "date_to", "reason"]
    assert len(ANTHROPIC_TOOLS) == 6


def test_agent_turn_round_trips_tool_use_and_result(tmp_path):
    client = FakeAnthropic([
        [text("Let me check."), tool_use("verify_patient", {"full_name": "Maria Gonzalez", "dob": "1985-03-14"})],
        [text("Thanks Maria, you're verified.")],
    ])
    a = Agent(Clinic.from_seed(), anthropic_model(client), tmp_path / "t.jsonl", system_prompt="SYS")
    assert a.turn("I'm Maria Gonzalez, 14 March 1985") == "Thanks Maria, you're verified."
    assert a.state.verified and a.state.patient_id == "PT001"  # the Gemini-side loop ran the tool and applied state
    first, second = client.requests
    assert first["system"].startswith("SYS") and first["extra_body"] == {"temperature": 0} and first["tool_choice"] == {"type": "auto"}
    assert first["messages"] == [{"role": "user", "content": [{"type": "text", "text": "I'm Maria Gonzalez, 14 March 1985"}]}]
    user, assistant, result = second["messages"]
    assert assistant["role"] == "assistant" and assistant["content"][0] == {"type": "text", "text": "Let me check."}
    use = assistant["content"][1]
    assert use["type"] == "tool_use" and use["name"] == "verify_patient" and use["input"]["dob"] == "1985-03-14"
    assert result["role"] == "user" and result["content"][0]["type"] == "tool_result"
    assert result["content"][0]["tool_use_id"] == use["id"]
    assert '"patient_id": "PT001"' in result["content"][0]["content"]


def test_tool_choice_none_after_cap_and_empty_model_turn_dropped(tmp_path):
    client = FakeAnthropic([[], [text("ok")], [text("still here")]])
    a = Agent(Clinic.from_seed(), anthropic_model(client), tmp_path / "t.jsonl", system_prompt="SYS")
    assert a.turn("hi") == ""
    a.turn("hello?")
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "user"]  # empty assistant turn not sent
    a.model(a.history, "SYS", False)  # extra call with tools disabled, as the loop does past MAX_TOOL_CALLS
    assert client.requests[-1]["tool_choice"] == {"type": "none"}
