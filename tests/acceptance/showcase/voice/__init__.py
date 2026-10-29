"""The voice step: a showcase video's narration clips, made with Amazon Polly (#1066).

``python -m showcase.voice <slug|all> --voice Ruth [--cache DIR]`` (``make
showcase-voice VIDEO=...``) runs on the operator's own machine, before any
capture, and is the only part of the showcase tool that calls AWS: ``make
showcase`` (capture and render) never does. For every ``say:`` sentence of the
storyboards it

1. checks the narration again (``showcase.capture.narration``: G1 and G3), and
   the pronunciation table (``contract.SPOKEN``), before any call;
2. looks the sentence's clip up in the cache (``cache``), outside every
   repository, by a key over everything the clip is made from (G8);
3. for each missing clip, within a cap on the characters one run may send
   (G18), asks Polly (``polly``: the operator's ``AWS_PROFILE``, an exact
   environment, errors reported by class only) and stretches the answer to 90%
   pace once (``audio``: G9), then files it in the cache, never overwriting;
4. measures every clip's loudness and true peak with the gain the render will
   give its video (``contract.voice_gain_db``: G10) and projects each video's
   length from its clips against the cap.

Exit status: 0 when every clip is in the cache and passes; 1 when a clip, a
cache entry or a video is refused (each reason printed); 2 when the run is
refused before it measures anything (usage, the narration, tools, the cache
directory, AWS settings, the character cap, or a failed Polly call).
"""
