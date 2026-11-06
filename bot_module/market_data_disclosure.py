# bot_module/market_data_disclosure.py
"""What the customer has to be told about the window a backtest was run over.

The loader records, on every frame it returns, WHERE the candles came from
and WHICH window they cover.  The KPI dict a run saves carries the frames
that do NOT cover the window that was asked for (the ``data_window``
notes), so a caller that reads it knows what is MISSING.  What that list
cannot say is where the candles came from when there was nothing missing,
and it says it by ABSENCE -- so a reader who finds no note learns only
that nobody looked.

This module puts the WHOLE truth on the KPI dict the run saves: one
record per frame, saying where its candles came from and whether the
window it covers is the window that was asked for.  A frame that covers
its window is reported as COMPLETE, so the absence of a warning is a
statement and not a silence.

THE GAP IS NOT MEASURED HERE.  The loader measured it -- it is the only
place that knows how a frame was assembled -- and this module carries the
loader's own note.  A second measurement would be a second thing that can
be wrong (see AGENTS.md: a predicate that is duplicated must handle the
same special cases in every copy).

THE LOADER'S NOTES ARE THE RECORD OF THE GAP.  They are written by
``data_loader.attach_klines_window_coverage`` and they arrive here on the
KPI dict they were written on, so the numbers the customer is told are the
numbers the loader measured.  A frame the loader says is short is never
reported as COMPLETE, even when its note is missing: "nobody looked" must
not be reported as "nothing was missing".

THE SENTENCE THE CUSTOMER READS IS BUILT BY THE CUSTOMER'S OWN SCREEN.
The loader's ``reason`` is an English sentence for the log; a screen that
speaks two languages needs the FACTS, not the sentence, or the two
languages would drift apart.
"""

import logging
from typing import Any, Dict, List, Optional

from .data_loader import klines_provenance, klines_window_coverage

logger = logging.getLogger(__name__)


def candles_disclosure(
    frames: Optional[Dict[str, Any]],
    gap_notes: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """One record per frame: where its candles came from, and whether the
    window it covers is the window that was asked for.

    ``frames`` maps a data key to the frame that was loaded ("kline_1h" ->
    the hourly frame, and so on).  ``gap_notes`` are the notes the loader
    wrote about the frames that do NOT cover their window; they are carried
    here as they are, because the loader measured them.

    A frame that carries no provenance is left out: it says nothing about
    itself, and a record invented for it would be a claim nobody checked.
    """
    if not frames:
        return []

    notes_by_key: Dict[str, Dict[str, Any]] = {}
    for note in gap_notes or []:
        if isinstance(note, dict) and note.get("data_key") is not None:
            notes_by_key[str(note["data_key"])] = note

    records: List[Dict[str, Any]] = []
    for key, frame in frames.items():
        provenance = klines_provenance(frame)
        if provenance is None:
            continue
        record: Dict[str, Any] = {
            "data_key": str(key),
            "source": provenance.get("source"),
            "requested_start": provenance.get("requested_start"),
            "requested_end": provenance.get("requested_end"),
            "actual_start": provenance.get("actual_start"),
            "actual_end": provenance.get("actual_end"),
            "rows": provenance.get("rows"),
            "differs_from_rest": bool(provenance.get("differs_from_rest")),
        }
        note = notes_by_key.get(str(key))
        if note is not None:
            # THE GAP IS THE LOADER'S OWN NOTE, carried as it is.
            record.update(note)
            record["complete"] = False
        elif klines_window_coverage(frame) is not None:
            # THE PREDICATE IS ASKED AGAIN AND THE ANSWER IS BELIEVED.  A
            # frame the loader says is short must NEVER be reported as
            # COMPLETE, even when the note that carries the numbers is
            # missing: the note may be missing because nobody asked for
            # it, and "nobody looked" must not be reported as "nothing was
            # missing".
            record["complete"] = False
        else:
            # THE WINDOW WAS COVERED.  Saying so is not the same as saying
            # nothing: the reader of a result that carries no warning has
            # to be able to tell "nothing was missing" from "nobody looked".
            record["complete"] = True
        records.append(record)

    # THE FRAMES THAT ARE INCOMPLETE COME FIRST.  The gap is the reason
    # this list exists, so it is the first thing a reader of it must see.
    records.sort(key=lambda record: bool(record.get("complete", True)))
    return records


def attach_candles_disclosure(
    kpis: Optional[Dict[str, Any]],
    frames: Optional[Dict[str, Any]],
    log_prefix: str = "",
) -> List[Dict[str, Any]]:
    """Puts ON the KPI dict a run saves what the customer has to be told
    about the window the numbers were computed over.

    The KPI dict already carries the loader's notes about the frames that
    do not cover their window, under ``data_window``; this replaces that
    list with the WHOLE truth -- every frame, the source it came from and
    whether it covered the window -- so one list answers both questions
    and neither of them is answered by silence.
    """
    if not isinstance(kpis, dict):
        return []

    records = candles_disclosure(frames, kpis.get("data_window"))
    if not records:
        return []

    kpis["data_window"] = records
    for record in records:
        if record.get("complete"):
            logger.info(
                f"{log_prefix}The candles of {record.get('data_key')} came from "
                f"{record.get('source')} and cover the window that was asked for."
            )
        else:
            logger.warning(
                f"{log_prefix}The candles of {record.get('data_key')} came from "
                f"{record.get('source')} and do NOT cover the window that was "
                f"asked for: {record.get('reason')}"
            )
    return records
