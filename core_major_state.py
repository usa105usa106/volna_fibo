"""Pure state transitions. Callers must validate candle integrity before entry."""

from __future__ import annotations

import pandas as pd

from core_major_rules import INVALID_TEXT, box_upper

SCORE_MODEL = "btc-structural-envelope-v0026"


def retire(book: dict, variant: str, reason: str, at: str) -> None:
    book.setdefault("retired", {}).setdefault(variant, {"reason": reason, "at": at})


def btc_hard_rules(book: dict, low: float, high: float, at: str) -> None:
    a, retired = book["anchors"], book.setdefault("retired", {})
    lower, upper = a["i1"]["price"], box_upper(a, book["v1_low"])
    if low < lower:
        retire(book, "V1", "W3-(4) / W3-(1) overlap", at)
    if high > upper:
        retire(book, "V1", "W3-(3) would be shortest of 1/3/5", at)
    if low < a["w2"]["price"]:
        retire(book, "V2", "W3-(2) broke W3-(1) origin", at)
    if low < a["origin"]["price"]:
        retire(book, "V3", "Senior W2 broke W1 origin", at)
    if "V1" not in retired:
        book.update(
            state="BTC_V1_BOX",
            primary="V1",
            alt=[v for v in ("V2", "V3") if v not in retired],
            unresolved=False,
        )
    elif "V2" in retired and "V3" in retired:
        book.update(
            state="BTC_FULL_RECOUNT",
            primary=None,
            alt=[],
            unresolved=False,
            current=INVALID_TEXT,
        )
    elif "V2" in retired:
        book.update(
            state="BTC_V3_PRIMARY",
            primary="V3",
            alt=[],
            unresolved=False,
            why="V2 hard-invalid; V3 survives",
        )
    elif "V3" in retired:
        book.update(
            state="BTC_V2_PRIMARY",
            primary="V2",
            alt=[],
            unresolved=False,
            why="V3 hard-invalid; V2 survives",
        )
    else:
        if book.get("primary") not in {"V2", "V3"}:
            book.update(
                state="BTC_POSTBOX_DUAL",
                primary=None,
                alt=["V2", "V3"],
                unresolved=True,
            )
    if len(retired) and book.get("primary") not in {"V2", "V3"}:
        book.pop("challenger", None)
        book["streak"] = 0


def choose_btc(book: dict, scores: dict, bucket: str) -> None:
    """Two DIFFERENT consecutive C4H buckets, never duplicate polling, elect a challenger."""
    if book.get("score_model") != SCORE_MODEL:
        # Recompute the old 0-vs-9 decision even within the same C4H bucket.
        # Keep PRIMARY and the permanent hard-invalid ledger, discard ONLY the
        # soft streak accumulated under an obsolete evidence search.
        book["score_model"] = SCORE_MODEL
        book.pop("score_bucket", None)
        book.pop("challenger", None)
        book["streak"] = 0
    book["scores"] = scores
    if "V1" not in book.get("retired", {}) or any(
        v in book["retired"] for v in ("V2", "V3")
    ):
        return
    if book.get("score_bucket") == bucket:
        return
    v2, v3 = scores["V2"]["total"], scores["V3"]["total"]
    if v2 is None or v3 is None:
        # UNKNOWN subdivision is neither a zero score nor hard invalidation.
        # Do not elect a soft challenger from an incomplete comparison.
        if book.get("primary") not in {"V2", "V3"}:
            book["primary"] = "V2" if v2 is not None and v3 is None else "V3"
        book.update(
            state="BTC_POSTBOX_DUAL",
            unresolved=True,
            alt=[v for v in ("V2", "V3") if v != book["primary"]],
            score_bucket=bucket,
            streak=0,
            why="Incomplete subdivision evidence; working PRIMARY, conditional ALT retained; no numeric score comparison",
        )
        book.pop("challenger", None)
        return
    winner = "V2" if v2 > v3 else "V3"
    diff = abs(v2 - v3)
    book["unresolved"] = diff < 2
    prior_bucket = book.get("score_bucket")
    consecutive = prior_bucket is not None and pd.Timestamp(bucket) - pd.Timestamp(
        prior_bucket
    ) == pd.Timedelta(hours=4)
    if book.get("primary") is None:
        book["primary"] = winner
        book["streak"] = 0
    elif winner != book["primary"] and diff >= 2:
        book["streak"] = (
            book.get("streak", 0) + 1
            if consecutive and book.get("challenger") == winner
            else 1
        )
        book["challenger"] = winner
        if book["streak"] >= 2:
            book["primary"] = winner
            book["streak"] = 0
            book.pop("challenger", None)
    else:
        book["streak"] = 0
        book.pop("challenger", None)
    book["score_bucket"] = bucket
    book["alt"] = [v for v in ("V2", "V3") if v != book["primary"]]
    book["state"] = (
        "BTC_POSTBOX_DUAL" if book["unresolved"] else f"BTC_{book['primary']}_PRIMARY"
    )
    book["why"] = (
        "V3 on equal score: simpler full-1D degree"
        if v2 == v3
        else "higher structural score"
    )
    if book["primary"] != winner:
        book["why"] = (
            "PRIMARY retained: score advantage is below the 2-point switch threshold"
        )
    if book["unresolved"]:
        book["why"] += "; both counts remain live, degree UNRESOLVED"
    if book.get("challenger"):
        book["why"] = (
            "PRIMARY retained: challenger needs two consecutive COMPLETE4H buckets"
        )
    book["why"] += f" (V2={v2:g}, V3={v3:g}, difference={diff:g})"


def eth_hard_rules(book: dict, low: float, at: str) -> None:
    if low < book["anchors"]["w2"]["price"]:
        retire(book, "ETH", "W3-(2) broke W3-(1) origin", at)
    if "ETH" in book.get("retired", {}):
        book.update(state="ETH_FULL_RECOUNT", current=INVALID_TEXT)


def reanchor(book: dict, low: float, bucket: str) -> bool:
    if book["state"] in {"BTC_FULL_RECOUNT", "ETH_FULL_RECOUNT"}:
        return False
    if low >= book["working_low"]:
        return False
    book.update(working_low=low, working_low_ts=bucket, accepted_at=None)
    if book["base"] == "BTC" and "V1" not in book.get("retired", {}):
        book["v1_low"] = low
    return True
