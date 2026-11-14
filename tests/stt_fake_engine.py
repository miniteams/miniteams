# Copied from grill-room `stt/tests/fake_engine.py` @ 89744d1 (spec 044, AC16), modified:
# imports point at miniteams.stt.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""A stand-in engine process for the pool tests: the real protocol, no model.

audio[0] picks the behaviour: 2 dies mid-utterance, 3 hangs; anything else answers "line <audio[1]>".
"""

import os
import sys
import threading

from miniteams.stt.engines import serve
from miniteams.stt.line import Line
from miniteams.stt.vad import Audio

DIE, HANG = 2.0, 3.0


def transcribe(audio: Audio, _prompt: str) -> Line:
    if audio[0] == DIE:
        os._exit(3)
    if audio[0] == HANG:
        threading.Event().wait()
    return Line(f"line {float(audio[1]):g}", "en")


if __name__ == "__main__":
    serve(transcribe, int(sys.argv[1]), int(sys.argv[2]))
