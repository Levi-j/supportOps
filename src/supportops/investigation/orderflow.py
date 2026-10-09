from supportops.http_checks import WRITE_METHODS
from supportops.investigation.collect import ORDER_PATH
from supportops.investigation.live import ORDER_CHECK, ORDER_CONDITION_TEXT, ORDER_EVENTS
from supportops.investigation.models import Confidence, Escalation, Finding, OrderState
from supportops.investigation.rules import (
    CURRENT_STATE_CAVEAT,
    Assessment,
    Entry,
    Facts,
    Rule,
    assess,
    check_key,
    finding,
    http_error_unexplained,
    request_succeeded,
    resource_not_found,
    unhandled_exception,
)
from supportops.investigation.text import describe_request
from supportops.logs.analysis import event_kind
from supportops.logs.parser import LogEvent

LOGIN_PATH = "/api/v1/auth/login"
INVENTORY_CHECK = "orderflow.inventory_mismatch"
DO_NOT_CORRECT = "Don't correct order or stock data from support; hand it to the service's owners."


def insufficient_stock(facts: Facts) -> Finding | None:
    entries = facts.entries("inventory.insufficient_stock")
    if not entries:
        return None
    key, event = entries[-1]
    assessment = assess(facts, key, {409})
    product = event.extra.get("productId", "unknown")
    change = event.extra.get("quantityChange", "unknown")
    assessment.inferences.append(
        "The service refuses any stock change that would make stock negative, so this is its "
        "normal protection rather than a fault. Other orders or adjustments may have used the "
        "remaining stock."
    )
    return finding(
        "insufficient_stock",
        "There wasn't enough stock for the request",
        f"The service refused the request because product {product} didn't have enough stock "
        f"for a change of {change}.",
        assessment,
        next_steps=[
            "Tell the customer the product isn't available in the requested quantity.",
            "If the stock level itself looks wrong, compare it with its movement history: "
            f"supportops db run {INVENTORY_CHECK}",
        ],
    )


def login_failed(facts: Facts) -> Finding | None:
    entries = facts.entries("auth.login_failed")
    if not entries:
        return None
    key, _event = entries[-1]
    assessment = assess(facts, key, {401})
    assessment.inferences.append(
        "The service deliberately gives the same answer for an unknown email and a wrong "
        "password, and doesn't log which one it was."
    )
    return finding(
        "login_failed",
        "The login was rejected",
        "The service logged auth.login_failed: the email and password didn't match an account.",
        assessment,
        next_steps=[
            "Ask the customer to re-enter or reset their password through the normal process; "
            "never ask for the password itself."
        ],
    )


def credentials_rejected(facts: Facts) -> Finding | None:
    access = facts.access()
    if not access or facts.entries("auth.login_failed"):
        return None
    key, event = access[-1]
    if event.status != 401 or event.path == LOGIN_PATH:
        return None
    assessment = Assessment(Confidence.POSSIBLE, [key])
    assessment.caveats.append(
        "Only the HTTP status is known: the service doesn't log why it rejected a bearer token."
    )
    assessment.inferences.append(
        "Common causes are an expired token (tokens last 30 minutes), a token issued by another "
        "environment, or no token at all. The response's WWW-Authenticate header tells a refused "
        "token from a missing one, but it isn't logged."
    )
    return finding(
        "credentials_rejected",
        "The service rejected the request's credentials",
        f"The service answered {describe_request(event)} without logging a reason.",
        assessment,
        next_steps=[
            "Ask the customer when the failure happened and whether the token was freshly "
            "issued; never ask for the token itself.",
            "If you can use the same token (for example from a test account), inspect its claims "
            "locally: supportops auth check --key-env VARIABLE",
        ],
    )


def order_inconsistent(facts: Facts) -> Finding | None:
    state = facts.live.order
    if state is None or state.lookup != "found" or not state.conditions or state.record is None:
        return None
    order_id = state.order_id
    links = _order_links(facts, order_id)
    confidence = Confidence.LIKELY if state.uncorroborated else Confidence.CONFIRMED
    assessment = Assessment(confidence, [check_key(ORDER_CHECK)])
    for name in state.corroborated_by:
        assessment.cite(check_key(name))
    for key, _event in links:
        assessment.cite(key)
    assessment.contradictions.extend(state.contradictions)
    assessment.caveats.append(CURRENT_STATE_CAVEAT)
    for name in state.uncorroborated:
        assessment.caveats.append(
            f"{name} didn't corroborate the order's record, so this rests on the order lookup "
            "alone."
        )
    _note_inventory(facts, assessment)
    assessment.inferences.append(_cause(facts, state, links))
    record = state.record
    summary = (
        f"Order {order_id} is inconsistent in the database now: "
        + "; ".join(ORDER_CONDITION_TEXT[name] for name in state.conditions)
        + f". Current record: status {record.get('status')}; total {record.get('total_amount')}; "
        f"items total {record.get('items_total')} across {record.get('item_count')} items; "
        f"units ordered {record.get('units_ordered')}, reserved {record.get('units_reserved')}, "
        f"restored {record.get('units_restored')}."
    )
    return finding(
        "order_inconsistent",
        f"Order {order_id} has inconsistent order or stock data",
        summary,
        assessment,
        next_steps=[
            DO_NOT_CORRECT,
            "Don't ask the customer to repeat the operation until the service's owners have "
            "reviewed the order.",
            f"After any fix, re-check the order: supportops db run {ORDER_CHECK} "
            f"--param id={order_id}",
        ],
        escalation=Escalation(
            team="Engineering / data owner",
            severity="high",
            reason="Order and stock data are inconsistent; correcting them needs the service's "
            "owners and a reviewed fix.",
        ),
    )


def _order_links(facts: Facts, order_id: str) -> list[Entry]:
    def names_order(event: LogEvent) -> bool:
        match = ORDER_PATH.match(event.path) if event.path else None
        in_path = match is not None and match.group(1) == order_id
        return in_path or str(event.extra.get("orderId")) == order_id

    return facts.matching(names_order)


def _cause(facts: Facts, state: OrderState, links: list[Entry]) -> str:
    order_id = state.order_id
    access = facts.access()
    status = access[-1][1].status if access else None
    write = facts.method in WRITE_METHODS
    failed = (status is not None and status >= 500) or bool(
        facts.matching(lambda event: event_kind(event) == "exception")
    )
    if write and failed:
        shown = f"HTTP {status}" if status is not None else "an error"
        return (
            f"Cause, possible association only: this request failed ({shown}) while changing "
            f"order {order_id}. The service writes orders and stock movements in one transaction, "
            "so a failed request should have changed nothing. The matching time, endpoint and "
            "order alone don't show that it caused the inconsistency; engineering needs to "
            "investigate."
        )
    events = sorted(
        {event.event_name or "" for _key, event in links if event.event_name in ORDER_EVENTS}
    )
    record = state.record or {}
    compare = (
        f" Compare the order's updated_at ({record.get('updated_at')}) and version "
        f"({record.get('version')}) with the time of this request."
    )
    if events:
        return (
            f"Cause not established: this request's logs show a normal {', '.join(events)} for "
            f"order {order_id}. They don't show it causing the inconsistency; a later change "
            "(another request, a manual database change or a migration) is at least as "
            f"plausible.{compare}"
        )
    if write:
        shown = f"HTTP {status}" if status is not None else "no recorded status"
        return (
            f"Cause not established: this request tried to change order {order_id} and ended "
            f"with {shown}, and its logs show no change to the order.{compare}"
        )
    return (
        f"Cause not established: this request only read order {order_id}, so it didn't change it."
    )


def _note_inventory(facts: Facts, assessment: Assessment) -> None:
    result = facts.checked(INVENTORY_CHECK)
    if result is None or not result.rows:
        return
    count = f"at least {result.row_count}" if result.truncated else str(result.row_count)
    assessment.caveats.append(
        f"{INVENTORY_CHECK} also lists {count} product(s) whose stock differs from their "
        "movements. They aren't attributed to this order."
    )


ORDERFLOW_RULES: tuple[Rule, ...] = (
    insufficient_stock,
    login_failed,
    credentials_rejected,
    order_inconsistent,
    unhandled_exception,
    resource_not_found,
    request_succeeded,
)
ORDERFLOW_FALLBACK_RULES: tuple[Rule, ...] = (http_error_unexplained,)
