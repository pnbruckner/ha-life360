"""Life360 common test functions."""
from __future__ import annotations

from collections.abc import Iterable
import re

import pytest


def assert_log_messages(
    caplog: pytest.LogCaptureFixture,
    messages: Iterable[tuple[int, str, str | re.Pattern]],
) -> None:
    """Check that log contains / doesn't contain specified messages.

    messages: [(expected, LevelName, str | pattern)]
    """
    records = caplog.get_records("call")
    for expected, level_name, str_pat in messages:
        rec_msgs = (rec.message for rec in records if rec.levelname == level_name)
        if isinstance(str_pat, re.Pattern):
            count = sum(bool(str_pat.fullmatch(rec_msg)) for rec_msg in rec_msgs)
            test_str = str_pat.pattern
        else:
            count = sum(rec_msg == str_pat for rec_msg in rec_msgs)
            test_str = str_pat
        assert (
            count == expected
        ), f'{level_name} "{test_str}" found {count} time(s), expected {expected}'
