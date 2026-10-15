"""The showcase render: a capture directory in; a captioned MP4, its WebVTT file and a review page out
(and, for a narrated capture, the MP4 with its narration, an SRT file and a transcript).

``python -m showcase.render <capture dir> --out <dir>`` (with ``tests/acceptance``
on ``sys.path``) runs ``pipeline.render``. Each module is one part:

* ``timeline``: constant 30 fps from the capture's variable-rate frames, and the
  caption on each frame, in whole frames;
* ``templates`` and ``chromium``: the caption band and the cards, drawn by Chromium;
* ``encode``: the frame sequences and the one ffmpeg run (overlay, concat, x264);
* ``probe``: gates on the encoded file (length, format, faststart, black frames);
* ``vtt``: the WebVTT file and its strict parser;
* ``ocr``: the burned-in captions read back, and every distinct frame read for
  credentials and forbidden text;
* ``narration`` and ``sound``: a narrated video's sentence captions (WebVTT
  and SRT), transcript, narration track, mux and sound gates;
* ``review``: the review page;
* ``synthetic``: made-up capture directories, with planted defects, to prove
  each gate refuses what it should.
"""
