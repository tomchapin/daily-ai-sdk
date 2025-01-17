import aiohttp
import asyncio
import json
import random
import logging
import os
import re
import wave
from typing import AsyncGenerator
from PIL import Image

from dailyai.pipeline.pipeline import Pipeline
from dailyai.pipeline.frame_processor import FrameProcessor
from dailyai.services.daily_transport_service import DailyTransportService
from dailyai.services.azure_ai_services import AzureLLMService, AzureTTSService
from dailyai.services.fal_ai_services import FalImageGenService
from dailyai.services.open_ai_services import OpenAILLMService
from dailyai.services.deepgram_ai_services import DeepgramTTSService
from dailyai.services.elevenlabs_ai_service import ElevenLabsTTSService
from dailyai.pipeline.aggregators import (
    LLMAssistantContextAggregator,
    LLMContextAggregator,
    LLMUserContextAggregator,
    ParallelPipeline,
    UserResponseAggregator,
    LLMResponseAggregator,
)
from examples.support.runner import configure
from dailyai.pipeline.frames import (
    EndPipeFrame,
    LLMMessagesQueueFrame,
    TranscriptionQueueFrame,
    Frame,
    TextFrame,
    LLMFunctionCallFrame,
    LLMFunctionStartFrame,
    LLMResponseEndFrame,
    StartFrame,
    AudioFrame,
    SpriteFrame,
    ImageFrame,
    UserStoppedSpeakingFrame,
)
from dailyai.services.ai_services import FrameLogger, AIService

logging.basicConfig(format=f"%(levelno)s %(asctime)s %(message)s")
logger = logging.getLogger("dailyai")
logger.setLevel(logging.DEBUG)

sounds = {}
images = {}
sound_files = ["talking.wav", "listening.wav", "ding3.wav"]
image_files = ["grandma-writing.png", "grandma-listening.png"]
script_dir = os.path.dirname(__file__)

for file in sound_files:
    # Build the full path to the sound file
    full_path = os.path.join(script_dir, "assets", file)
    # Get the filename without the extension to use as the dictionary key
    filename = os.path.splitext(os.path.basename(full_path))[0]
    # Open the sound and convert it to bytes
    with wave.open(full_path) as audio_file:
        sounds[file] = audio_file.readframes(-1)

for file in image_files:
    # Build the full path to the image file
    full_path = os.path.join(script_dir, "assets", file)
    # Get the filename without the extension to use as the dictionary key
    filename = os.path.splitext(os.path.basename(full_path))[0]
    # Open the image and convert it to bytes
    with Image.open(full_path) as img:
        images[file] = img.tobytes()


class StoryStartFrame(TextFrame):
    pass


class StoryPageFrame(TextFrame):
    pass


class StoryPromptFrame(TextFrame):
    pass


class StoryProcessor(FrameProcessor):
    def __init__(self, messages, story):
        self._messages = messages
        self._text = ""
        self._story = story

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        """
        The response from the LLM service looks like:
        A comment about the user's choice
        [start] (when the cat starts telling parts of the story)
        A sentence of the story
        [break] (between each sentence/'page' of the story)
        [prompt] (when the cat asks the user to make a decision)
        Question about the next part of the story

        1. Catch the frames that are generated by the LLM service
        """
        if isinstance(frame, UserStoppedSpeakingFrame):
            yield ImageFrame(None, images["grandma-writing.png"])
            yield AudioFrame(sounds["talking.wav"])

        elif isinstance(frame, TextFrame):
            self._text += frame.text

            if re.findall(r".*\[[sS]tart\].*", self._text):
                # Then we have the intro. Send it to speech ASAP
                self._text = self._text.replace("[Start]", "")
                self._text = self._text.replace("[start]", "")

                self._text = self._text.replace("\n", " ")
                if len(self._text) > 2:
                    yield ImageFrame(None, images["grandma-writing.png"])
                    yield StoryStartFrame(self._text)
                    yield AudioFrame(sounds["ding3.wav"])
                self._text = ""

            elif re.findall(r".*\[[bB]reak\].*", self._text):
                # Then it's a page of the story. Get an image too
                self._text = self._text.replace("[Break]", "")
                self._text = self._text.replace("[break]", "")
                self._text = self._text.replace("\n", " ")
                if len(self._text) > 2:
                    self._story.append(self._text)
                    yield StoryPageFrame(self._text)
                    yield AudioFrame(sounds["ding3.wav"])

                self._text = ""
            elif re.findall(r".*\[[pP]rompt\].*", self._text):
                # Then it's question time. Flush any
                # text here as a story page, then set
                # the var to get to prompt mode
                # cb: trying scene now
                # self.handle_chunk(self._text)
                self._text = self._text.replace("[Prompt]", "")
                self._text = self._text.replace("[prompt]", "")

                self._text = self._text.replace("\n", " ")
                if len(self._text) > 2:
                    self._story.append(self._text)
                    yield StoryPageFrame(self._text)
            else:
                # After the prompt thing, we'll catch an LLM end to get the
                # last bit
                pass
        elif isinstance(frame, LLMResponseEndFrame):
            yield ImageFrame(None, images["grandma-writing.png"])
            yield StoryPromptFrame(self._text)
            self._text = ""
            yield frame
            yield ImageFrame(None, images["grandma-listening.png"])
            yield AudioFrame(sounds["listening.wav"])

        else:
            # pass through everything that's not a TextFrame
            yield frame


class StoryImageGenerator(FrameProcessor):
    def __init__(self, story, llm, img):
        self._story = story
        self._llm = llm
        self._img = img

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        if isinstance(frame, StoryPageFrame):
            if len(self._story) == 1:
                prompt = f'You are an illustrator for a children\'s story book. Generate a prompt for DALL-E to create an illustration for the first page of the book, which reads: "{self._story[0]}"\n\n Your response should start with the phrase "Children\'s book illustration of".'
            else:
                prompt = f"You are an illustrator for a children's story book. Here is the story so far:\n\n\"{' '.join(self._story[:-1])}\"\n\nGenerate a prompt for DALL-E to create an illustration for the next page. Here's the sentence for the next page:\n\n\"{self._story[-1:][0]}\"\n\n Your response should start with the phrase \"Children's book illustration of\"."
            msgs = [{"role": "system", "content": prompt}]
            image_prompt = ""
            async for f in self._llm.process_frame(LLMMessagesQueueFrame(msgs)):
                if isinstance(f, TextFrame):
                    image_prompt += f.text
            async for f in self._img.process_frame(TextFrame(image_prompt)):
                yield f
            # Yield the original StoryPageFrame for basic image/audio sync
            yield frame
        else:
            yield frame


async def main(room_url: str, token):
    async with aiohttp.ClientSession() as session:
        messages = [
            {
                "role": "system",
                "content": "You are a storytelling grandma who loves to make up fantastic, fun, and educational stories for children between the ages of 5 and 10 years old. Your stories are full of friendly, magical creatures. Your stories are never scary. Each sentence of your story will become a page in a storybook. Stop after 3-4 sentences and give the child a choice to make that will influence the next part of the story. Once the child responds, start by saying something nice about the choice they made, then include [start] in your response. Include [break] after each sentence of the story. Include [prompt] between the story and the prompt.",
            }
        ]

        story = []

        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_CHATGPT_API_KEY"),
            model="gpt-4-1106-preview",
        )  # gpt-4-1106-preview
        tts = ElevenLabsTTSService(
            aiohttp_session=session,
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id="Xb7hH8MSUJpSbSDYk0k2",
        )  # matilda
        img = FalImageGenService(
            image_size="1024x1024",
            aiohttp_session=session,
            key_id=os.getenv("FAL_KEY_ID"),
            key_secret=os.getenv("FAL_KEY_SECRET"),
        )
        lra = LLMResponseAggregator(messages)
        ura = UserResponseAggregator(messages)
        sp = StoryProcessor(messages, story)
        sig = StoryImageGenerator(story, llm, img)

        transport = DailyTransportService(
            room_url,
            token,
            "Storybot",
            5,
            mic_enabled=True,
            mic_sample_rate=16000,
            camera_enabled=True,
            camera_width=1024,
            camera_height=1024,
            start_transcription=True,
            vad_enabled=True,
            vad_stop_s=1.5,
        )

        start_story_event = asyncio.Event()

        @transport.event_handler("on_first_other_participant_joined")
        async def on_first_other_participant_joined(transport):
            start_story_event.set()

        async def storytime():
            await start_story_event.wait()

            # We're being a bit tricky here by using a special system prompt to
            # ask the user for a story topic. After their intial response, we'll
            # use a different system prompt to create story pages.
            intro_messages = [
                {
                    "role": "system",
                    "content": "You are a storytelling grandma who loves to make up fantastic, fun, and educational stories for children between the ages of 5 and 10 years old. Your stories are full of friendly, magical creatures. Your stories are never scary. Begin by asking what a child wants you to tell a story about. Keep your reponse to only a few sentences.",
                }
            ]
            lca = LLMAssistantContextAggregator(messages)
            local_pipeline = Pipeline(
                [llm, lca, tts], sink=transport.send_queue)
            await local_pipeline.queue_frames(
                [
                    ImageFrame(None, images["grandma-listening.png"]),
                    LLMMessagesQueueFrame(intro_messages),
                    AudioFrame(sounds["listening.wav"]),
                    EndPipeFrame(),
                ]
            )
            await local_pipeline.run_pipeline()

            fl = FrameLogger("### After Image Generation")
            pipeline = Pipeline(
                processors=[
                    ura,
                    llm,
                    sp,
                    sig,
                    fl,
                    tts,
                    lra,
                ]
            )
            await transport.run_pipeline(
                pipeline,
            )

        transport.transcription_settings["extra"]["endpointing"] = True
        transport.transcription_settings["extra"]["punctuate"] = True
        try:
            await asyncio.gather(transport.run(), storytime())
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("whoops")
            transport.stop()


if __name__ == "__main__":
    (url, token) = configure()
    asyncio.run(main(url, token))
