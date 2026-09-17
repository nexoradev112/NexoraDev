import os
from pathlib import Path

from groq import Groq

client = Groq(api_key=os.environ["GROQ_API_KEY"])

speech_file_path = str(Path(__file__).with_name("orpheus-english.wav")) 
model = "canopylabs/orpheus-v1-english"
voice = "troy"
text = "Welcome to Orpheus text-to-speech. [cheerful] This is an example of high-quality English audio generation with vocal directions support."
response_format = "wav"

response = client.audio.speech.create(
    model=model,
    voice=voice,
    input=text,
    response_format=response_format
)

response.write_to_file(speech_file_path)
print(f"Wrote {speech_file_path}")
