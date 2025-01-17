import asyncio
import os
import logging

import aiohttp

from dailyai.pipeline.frames import EndFrame, LLMMessagesQueueFrame
from dailyai.pipeline.pipeline import Pipeline
from dailyai.services.daily_transport_service import DailyTransportService
from dailyai.services.elevenlabs_ai_service import ElevenLabsTTSService
from dailyai.services.open_ai_services import OpenAILLMService

from examples.support.runner import configure

logging.basicConfig(format=f"%(levelno)s %(asctime)s %(message)s")
logger = logging.getLogger("dailyai")
logger.setLevel(logging.DEBUG)


async def main(room_url):
    async with aiohttp.ClientSession() as session:
        transport = DailyTransportService(
            room_url,
            None,
            "Say One Thing From an LLM",
            mic_enabled=True,
        )

        tts = ElevenLabsTTSService(
            aiohttp_session=session,
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
        )

        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_CHATGPT_API_KEY"),
            model="gpt-4-turbo-preview")

        messages = [
            {
                "role": "system",
                "content": "You are an LLM in a WebRTC session, and this is a 'hello world' demo. Say hello to the world.",
            }]

        pipeline = Pipeline([llm, tts])

        @transport.event_handler("on_first_other_participant_joined")
        async def on_first_other_participant_joined(transport):
            await pipeline.queue_frames([LLMMessagesQueueFrame(messages), EndFrame()])

        await transport.run(pipeline)


if __name__ == "__main__":
    (url, token) = configure()
    asyncio.run(main(url))
