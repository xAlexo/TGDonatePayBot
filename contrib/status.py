from enum import IntEnum


class Status(IntEnum):
    NOT_SET = 2
    WAIT_CHANNEL = 3
    WAIT_DP_API_KEY = 5
