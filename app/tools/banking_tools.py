"""Thin tool functions the banking agent can call.

Transfers are a two-step, code-enforced flow: `initiate_transfer` validates
and stages a request, `confirm_transfer` executes only a request that was
actually staged. The model cannot talk its way past a rejected, ambiguous,
or unconfirmed request.
"""

from __future__ import annotations

from typing import Any

from app import audit, limits, mock_data
from app.services import policy_service, transaction_executor, transfer_service


def get_balance() -> dict[str, Any]:
    """Return the balance, status, and daily transfer limit usage for the user's savings account."""
    account = mock_data.accounts[mock_data.ACCOUNT_ID]
    return {
        "account": mock_data.ACCOUNT_ID,
        "balance": account["balance"],
        "status": account["status"],
        "daily_limit": account["daily_limit"],
        "daily_used": account["daily_used"],
    }


def find_beneficiaries(query: str) -> dict[str, Any]:
    """Search beneficiaries by name, id, or last-4 account digits."""
    matches = mock_data.resolve_beneficiaries(query)
    return {"query": query, "matches": [mock_data.public_beneficiary(b) for b in matches]}


def initiate_transfer(beneficiary: str, amount: float) -> dict[str, Any]:
    """Validate a transfer request and stage it for confirmation. Does not move money.

    The request is validated by PolicyService first. Possible outcomes:
    - NEEDS_CLARIFICATION: more than one beneficiary matches; nothing staged.
    - REJECTED: a deterministic rule failed (blocked beneficiary, amount,
      balance, or daily limit); nothing staged.
    - PENDING_CONFIRMATION: the request is valid and now staged. Tell the
      user the beneficiary, masked account, and amount, and ask them to
      confirm before calling confirm_transfer.
    - BLOCKED: a session limit (too many transfer attempts, or the same
      failing request repeated too many times) was reached; nothing staged.
    """
    if not limits.register_transfer_attempt():
        audit.log("LIMIT_EXCEEDED", limit="MAX_TRANSFER_ATTEMPTS")
        return {
            "status": "BLOCKED",
            "reason": "Maximum transfer attempts reached for this session.",
        }

    validation = policy_service.validate_transfer(beneficiary, amount)

    if validation.status == "NEEDS_CLARIFICATION":
        limits.record_transfer_outcome(is_rejection=False)
        audit.log("VALIDATION_FAILED", outcome="NEEDS_CLARIFICATION", beneficiary=beneficiary)
        return {
            "status": "NEEDS_CLARIFICATION",
            "candidates": [mock_data.public_beneficiary(b) for b in validation.candidates],
        }

    if validation.status == "REJECTED":
        within_limit = limits.record_transfer_outcome(is_rejection=True, signature=validation.reason)
        audit.log("VALIDATION_FAILED", outcome="REJECTED", reason=validation.reason)
        if not within_limit:
            audit.log("LIMIT_EXCEEDED", limit="MAX_IDENTICAL_FAILURES", reason=validation.reason)
            return {
                "status": "BLOCKED",
                "reason": f"Repeated the same failing request too many times ({validation.reason}).",
            }
        return {"status": "REJECTED", "reason": validation.reason}

    limits.record_transfer_outcome(is_rejection=False)
    audit.log("VALIDATION_PASSED", beneficiary=validation.beneficiary["name"], amount=amount)

    target = validation.beneficiary
    account_last4 = mock_data.mask_account_number(target["account_number"])
    transfer_service.set_pending(
        transfer_service.PendingTransfer(
            beneficiary_id=target["id"],
            beneficiary_name=target["name"],
            account_last4=account_last4,
            amount=amount,
        )
    )
    audit.log("CONFIRMATION_REQUESTED", beneficiary=target["name"], account_last4=account_last4, amount=amount)
    return {
        "status": "PENDING_CONFIRMATION",
        "beneficiary_name": target["name"],
        "account_last4": account_last4,
        "amount": amount,
    }


def confirm_transfer() -> dict[str, Any]:
    """Execute the currently staged transfer, if one exists. Takes no parameters.

    This only executes a transfer that was actually staged by a prior
    initiate_transfer call. If nothing is staged it is a no-op, regardless
    of what the user or model asked for.

    The returned status (SUCCESS, FAILED, or PENDING) is the verified
    outcome from transaction_executor, not an assumption that calling this
    function without an exception means the transfer succeeded. Report this
    status to the user exactly as returned — do not assume success.
    """
    pending = transfer_service.get_pending()
    if pending is None:
        return {
            "status": "NO_PENDING_TRANSFER",
            "reason": "There is no pending transfer to confirm.",
        }
    transfer_service.clear_pending()
    audit.log("TRANSFER_CONFIRMED", beneficiary=pending.beneficiary_name, amount=pending.amount)

    outcome = transaction_executor.execute()

    account = mock_data.accounts[mock_data.ACCOUNT_ID]
    if outcome in ("SUCCESS", "PENDING"):
        # A pending transfer still places a hold on the funds; a failed one
        # never leaves the account.
        account["balance"] -= pending.amount
        account["daily_used"] += pending.amount

    transaction_id = mock_data.next_transaction_id()
    mock_data.transactions[transaction_id] = {
        "id": transaction_id,
        "beneficiary_id": pending.beneficiary_id,
        "beneficiary_name": pending.beneficiary_name,
        "amount": pending.amount,
        "status": outcome,
    }
    audit.log("TRANSFER_EXECUTED", transaction_id=transaction_id, outcome=outcome)

    result = {
        "status": outcome,
        "transaction_id": transaction_id,
        "beneficiary_name": pending.beneficiary_name,
        "amount": pending.amount,
    }
    if outcome != "FAILED":
        result["new_balance"] = account["balance"]
    audit.log("TRANSACTION_VERIFIED", transaction_id=transaction_id, status=outcome)
    return result


def get_transaction_status(transaction_id: str) -> dict[str, Any]:
    """Look up the status of a previously executed transfer by transaction id."""
    transaction = mock_data.transactions.get(transaction_id)
    if transaction is None:
        return {"status": "ERROR", "reason": f"No transaction found with id '{transaction_id}'."}
    return transaction
