"""Server-side pronunciation is currently disabled.

The Piper implementation is intentionally kept out of the runtime. The web
client uses the browser's speech synthesis implementation instead.
"""

# Piper implementation (disabled):
#
# from piper import PiperVoice
#
# async def initialize_pronunciation_model() -> None:
#     voice = await asyncio.to_thread(
#         PiperVoice.load,
#         settings.piper_model_path,
#     )
#
# async def get_pronunciation_audio(word: str, ipa: str | None) -> bytes:
#     await initialize_pronunciation_model()
#     ...
