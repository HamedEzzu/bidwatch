from strands import Agent, tool
from strands.models import BedrockModel
from datetime import datetime
from config import MODEL_ID, REGION

@tool
def get_current_time() -> str:
    """Return the current date and time."""
    return datetime.now().isoformat()

agent = Agent(
    model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
    tools=[get_current_time],
    system_prompt="You are a helpful assistant. Use your tools when relevant.",
)

print(agent("What time is it right now?"))