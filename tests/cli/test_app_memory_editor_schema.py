from chat_agent.cli import app as app_module
from chat_agent.core.schema import AgentConfig, CodexConfig, DeepSeekConfig


def test_agent_supports_response_schema_requires_all_fallback_candidates():
    agent_config = AgentConfig(
        llm=CodexConfig(provider="codex", model="gpt-5.5"),
        llm_fallbacks=[
            DeepSeekConfig(
                provider="deepseek",
                model="deepseek-v4-flash",
                thinking={"enabled": True, "effort": "max"},
            )
        ],
    )

    assert app_module._agent_supports_response_schema(agent_config) is False
