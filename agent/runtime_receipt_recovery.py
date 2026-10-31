"""Finite reconciliation of an already journaled worker receipt, never a new action."""
import logging

logger = logging.getLogger(__name__)


def receipt_budget(method):
    from hermes_state import SessionDB
    # The server deliberately permits transcript writes to wait60s. A20s client
    # deadline can abandon a committed write before its receipt reaches the worker.
    patience = (SessionDB._TRANSCRIPT_WRITE_PATIENCE_S if method == 'worker.persist'
                else SessionDB._WRITE_PATIENCE_S)
    return patience + 30.0


def reconcile_journaled_receipt(rpc, scope, entry):
    """One lost-answer retry of the identical authenticated execution/sequence.

    The owner deduplicates that sequence+payload digest transactionally. Epoch,
    assignment and policy refusals are final; uncertainty after the second answer
    remains in the original durable outbox for explicit owner reconciliation.
    """
    from websockets.exceptions import ConnectionClosed
    try:
        return rpc('worker.persist', **scope, **entry)
    except (TimeoutError, OSError, ConnectionClosed):
        logger.warning('Worker receipt answer lost; reconciling the same journaled sequence once')
        return rpc('worker.persist', **scope, **entry)
