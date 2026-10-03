"""Purchase saga state machine. Transitions are monotonic: a late or
duplicated message can never move a purchase backwards."""

REQUESTED = "REQUESTED"
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
ESCALATED = "ESCALATED"
CHECKED = "CHECKED"
RESERVED = "RESERVED"
ORDER_CREATED = "ORDER_CREATED"
CAPTURED = "CAPTURED"
CONFIRMED = "CONFIRMED"
PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
REFUNDED = "REFUNDED"
DENIED = "DENIED"
EXPIRED = "EXPIRED"
FAILED = "FAILED"

TRANSITIONS = {
    REQUESTED: {CHECKED, DENIED, ESCALATED, AWAITING_CONFIRMATION},
    AWAITING_CONFIRMATION: {CHECKED, RESERVED, DENIED, ESCALATED, EXPIRED},
    ESCALATED: {CHECKED, DENIED, EXPIRED},
    CHECKED: {RESERVED, ESCALATED, DENIED},
    RESERVED: {ORDER_CREATED, AWAITING_CONFIRMATION, EXPIRED, FAILED, DENIED, ESCALATED},
    ORDER_CREATED: {CAPTURED, CONFIRMED, EXPIRED, FAILED},
    CAPTURED: {CONFIRMED},
    CONFIRMED: {PARTIALLY_REFUNDED, REFUNDED},
    PARTIALLY_REFUNDED: {REFUNDED},
}
TERMINAL = {DENIED, EXPIRED, FAILED, REFUNDED}
# Rank along the payment path, used to ignore stale/out-of-order webhooks.
PAYMENT_RANK = {RESERVED: 1, ORDER_CREATED: 2, CAPTURED: 3, CONFIRMED: 4, PARTIALLY_REFUNDED: 5, REFUNDED: 6}


class IllegalTransition(Exception):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def check_transition(src: str, dst: str) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(f"{src} -> {dst}")


def is_advance(current: str, target: str) -> bool:
    return PAYMENT_RANK.get(target, 0) > PAYMENT_RANK.get(current, 0)
