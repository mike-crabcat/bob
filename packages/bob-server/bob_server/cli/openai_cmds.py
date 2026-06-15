"""Bob CLI openai subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="OpenAI LLM evaluation commands")



@app.command("prompt")
def openai_prompt(
    prompt: Annotated[str, typer.Argument(help="Prompt text to send")],
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Model name")] = None,
    temperature: Annotated[float, typer.Option("--temperature", "-t", help="Sampling temperature")] = 0.7,
) -> None:
    """Send a prompt to OpenAI and print the response."""
    data: dict[str, Any] = {"prompt": prompt, "temperature": temperature}
    if model:
        data["model"] = model
    result = _api_call("POST", "/api/v1/openai/prompt", data)
    typer.echo(result["data"]["content"])


# ── Eval framework ──────────────────────────────────────────────────

