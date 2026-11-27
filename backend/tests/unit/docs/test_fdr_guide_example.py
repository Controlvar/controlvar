"""The Benjamini-Hochberg guide's worked example agrees with the service.

The guide's Result sentence once said ranks 3-5 fail their per-rank threshold
(rank 5 passes), and its last column said No for ranks 3 and 4, which BH
rejects. The table is read back from the page and checked against the
service, and the ranks the sentence names must be the computed ones.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.services.fdr_correction_service import BenjaminiHochbergService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

PAGE = Path(__file__).resolve().parents[4] / "docs/statistics/fdr-correction.md"

ROW = re.compile(
    r"^\| (\d+) \| \*{0,2}([\d.]+)\*{0,2} \| \*{0,2}([\d.]+)\*{0,2} "
    r"\| \*{0,2}(Yes|No)\*{0,2}[^|]*\| (Yes|No) \|$",
    flags=re.MULTILINE,
)


def test_the_table_and_the_result_sentence_match_the_service():
    text = PAGE.read_text(encoding="utf-8")
    rows = ROW.findall(text)
    assert [int(r[0]) for r in rows] == list(range(1, 11))

    results = BenjaminiHochbergService().correct(
        {f"m{r[0]}": float(r[1]) for r in rows}, fdr_threshold=0.10
    )
    rejected = {r.rank for r in results if r.is_significant}

    failing = []
    for rank, p, threshold, meets, rejects in rows:
        own = float(p) <= float(threshold)
        assert (meets == "Yes") is own, f"rank {rank}: its own threshold"
        assert (rejects == "Yes") is (int(rank) in rejected), f"rank {rank}"
        if not own and int(rank) in rejected:
            failing.append(int(rank))
    assert failing == [3, 4]

    sentence = re.search(r"even though ranks ([\d, and]+)\s+individually", text)
    assert sentence, "the Result sentence names no ranks"
    assert [int(n) for n in re.findall(r"\d+", sentence.group(1))] == failing
