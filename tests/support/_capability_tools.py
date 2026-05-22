"""Deterministic Tool subclasses for capability and tool-loop tests."""

from pydantic import BaseModel, Field

from ai_pipeline_core.llm import Tool


class GetCapital(Tool):
    """Get the capital city of a country."""

    class Input(BaseModel):
        """Input for the get_capital tool."""

        country: str = Field(description="Country name to look up.")

    class Output(BaseModel):
        """Output from the get_capital tool."""

        answer: str = Field(description="Capital lookup result text.")

    async def run(self, input: Input) -> Output:
        """Return the capital for a small set of known countries."""
        capitals = {"france": "Paris", "germany": "Berlin", "japan": "Tokyo"}
        city = capitals.get(input.country.lower())
        if city is None:
            return self.Output(answer=f"Unknown country: {input.country}")
        return self.Output(answer=f"The capital of {input.country} is {city}.")
