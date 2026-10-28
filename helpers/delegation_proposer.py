import os
import requests
from web3 import Web3
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from dotenv import load_dotenv
from helpers.decode_hex import decode_hex_data, get_function_name

# Load environment variables
load_dotenv()

BASE_URL = os.getenv("BASE_URL")
SAFE_ADDRESS = os.getenv("SAFE_ADDRESS")
STAKING_CONTRACT_ADDRESS = os.getenv("STAKING_CONTRACT_ADDRESS")
SONIC_RPC_URL = os.getenv("SONIC_RPC_URL")
SAFE_API_KEY = os.getenv("SAFE_API_KEY")  # optional, sent as bearer token to the Safe API
PROPOSER_KEY = os.getenv("STS_DELEGATION_PROPOSER")  # proposer (delegate) key of the Safe, cannot sign or execute

RECOMMENDATION_URL = "https://sts-helper.beets-ftm-node.com/api/stake-recommendation"
CHUNK_SIZE = 500_000      # S per delegate tx
MIN_DELEGATION = 10_000   # don't propose dust txs for nearly-filled gaps
REFILL_BELOW = 2          # top up once fewer pending delegate txs than this are queued
REFILL_TO = 8             # ...up to this many
DELEGATE_SELECTOR = "d9a34952"  # delegate(uint256,uint256)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
REQUEST_TIMEOUT = 15

SAFE_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"},
        ],
        "name": "getTransactionHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    }
]

STS_ABI = [{"inputs": [], "name": "SFC", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"}]
SFC_ABI = [
    {"inputs": [], "name": "constsAddress", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"type": "uint256"}],
        "name": "getValidator",
        "outputs": [
            {"name": "status", "type": "uint256"},
            {"name": "receivedStake", "type": "uint256"},
            {"name": "auth", "type": "address"},
            {"name": "createdEpoch", "type": "uint256"},
            {"name": "createdTime", "type": "uint256"},
            {"name": "deactivatedTime", "type": "uint256"},
            {"name": "deactivatedEpoch", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {"inputs": [{"type": "uint256"}], "name": "getSelfStake", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]
CONSTS_ABI = [{"inputs": [], "name": "maxDelegatedRatio", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}]

web3 = Web3(Web3.HTTPProvider(SONIC_RPC_URL))
safe_contract = web3.eth.contract(address=Web3.to_checksum_address(SAFE_ADDRESS), abi=SAFE_ABI)
sts_contract = web3.eth.contract(address=Web3.to_checksum_address(STAKING_CONTRACT_ADDRESS), abi=STS_ABI)


def _headers():
    return {"Authorization": f"Bearer {SAFE_API_KEY}"} if SAFE_API_KEY else {}


def _safe_api(path):
    response = requests.get(f"{BASE_URL}{path}", headers=_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_queue_state():
    """
    Returns (next_nonce, pending_delegations) where pending_delegations is a list of
    {"nonce", "validator_id", "amount"} for queued, unexecuted delegate txs (newest tx per nonce).
    """
    onchain_nonce = int(_safe_api(f"/api/v1/safes/{SAFE_ADDRESS}/")["nonce"])
    txs = _safe_api(
        f"/api/v1/safes/{SAFE_ADDRESS}/multisig-transactions/?executed=false&nonce__gte={onchain_nonce}&limit=100"
    )["results"]

    # Same rule as filter_and_sort_pending_transactions: only the newest tx per nonce counts
    latest = {}
    for tx in txs:
        if tx["nonce"] not in latest or tx["submissionDate"] > latest[tx["nonce"]]["submissionDate"]:
            latest[tx["nonce"]] = tx

    pending_delegations = []
    for nonce, tx in sorted(latest.items()):
        if not tx.get("data") or get_function_name(tx["data"]) != "delegate":
            continue
        decoded = decode_hex_data(tx["data"]) or {}
        pending_delegations.append({
            "nonce": nonce,
            "validator_id": decoded.get("validatorId"),
            "amount": float(decoded.get("amountInTokens", 0.0)),
        })

    next_nonce = max([onchain_nonce] + [nonce + 1 for nonce in latest])
    return next_nonce, pending_delegations


def get_sfc_room(validator_ids):
    """
    On-chain delegation room per validator in S: selfStake * maxDelegatedRatio - receivedStake (the SFC limit).
    Validators that are not active (status != 0) get 0.
    """
    sfc = web3.eth.contract(address=sts_contract.functions.SFC().call(), abi=SFC_ABI)
    consts = web3.eth.contract(address=sfc.functions.constsAddress().call(), abi=CONSTS_ABI)
    max_ratio = consts.functions.maxDelegatedRatio().call()

    room = {}
    for validator_id in validator_ids:
        status, received_stake, *_ = sfc.functions.getValidator(int(validator_id)).call()
        self_stake = sfc.functions.getSelfStake(int(validator_id)).call()
        limit = self_stake * max_ratio // 10**18
        room[validator_id] = (limit - received_stake) / 10**18 if status == 0 else 0.0
    return room


def plan_delegations(pending_delegations, count):
    """
    Pick `count` chunks for the validators with the largest gap left after the already queued delegations.
    Each chunk is capped by the validator's room: min(API remainingCapacity, on-chain SFC room) minus what is already queued.
    """
    response = requests.get(RECOMMENDATION_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()["data"]
    stake_more = data["recommendations"]["stakeMore"]
    api_capacity = {
        v["validatorId"]: float(v["remainingCapacity"]) if v["canReceiveDelegation"] else 0.0
        for group in data["validators"].values()
        for v in group
    }

    remaining = {rec["validatorId"]: float(rec["recommendedAmount"]) for rec in stake_more}
    sfc_room = get_sfc_room(remaining.keys())
    room = {v: min(api_capacity.get(v, 0.0), sfc_room[v]) for v in remaining}
    for queued in pending_delegations:
        if queued["validator_id"] in remaining:
            remaining[queued["validator_id"]] -= queued["amount"]
            room[queued["validator_id"]] -= queued["amount"]

    plan = []
    for _ in range(count):
        # Largest gap among validators that can still take at least MIN_DELEGATION
        candidates = {v: gap for v, gap in remaining.items() if room[v] >= MIN_DELEGATION}
        validator_id, gap = max(candidates.items(), key=lambda item: item[1], default=(None, 0))
        amount = int(min(CHUNK_SIZE, gap, room.get(validator_id, 0)))
        if amount < MIN_DELEGATION:
            break
        plan.append({"validator_id": validator_id, "amount": amount})
        remaining[validator_id] -= amount
        room[validator_id] -= amount
    return plan


def propose_delegation(nonce, validator_id, amount):
    """Propose delegate(validator_id, amount S) at `nonce` to the Safe queue, signed by the proposer key."""
    if not PROPOSER_KEY:
        raise ValueError("STS_DELEGATION_PROPOSER is not set")
    proposer = Account.from_key(PROPOSER_KEY)

    to = Web3.to_checksum_address(STAKING_CONTRACT_ADDRESS)
    data = bytes.fromhex(DELEGATE_SELECTOR) + encode(["uint256", "uint256"], [int(validator_id), amount * 10**18])
    safe_tx_hash = safe_contract.functions.getTransactionHash(
        to, 0, data, 0, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, nonce
    ).call()

    # eth_sign style signature (EIP-191 over the safeTxHash, v + 4), same as the Safe SDK uses for proposers
    signed = Account.sign_message(encode_defunct(primitive=safe_tx_hash), proposer.key)
    signature = (signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([signed.v + 4])).hex()

    body = {
        "to": to,
        "value": "0",
        "data": "0x" + data.hex(),
        "operation": 0,
        "safeTxGas": "0",
        "baseGas": "0",
        "gasPrice": "0",
        "gasToken": ZERO_ADDRESS,
        "refundReceiver": ZERO_ADDRESS,
        "nonce": nonce,
        "contractTransactionHash": "0x" + safe_tx_hash.hex(),
        "sender": proposer.address,
        "signature": "0x" + signature,
        "origin": "delegatooooor",
    }
    response = requests.post(
        f"{BASE_URL}/api/v1/safes/{SAFE_ADDRESS}/multisig-transactions/",
        json=body,
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Safe API rejected proposal for nonce {nonce}: {response.status_code} {response.text}")


def refill_delegation_queue(dry_run=False):
    """
    Top up the Safe queue with delegate txs once fewer than REFILL_BELOW are pending.
    Returns (proposed, error): proposed is the list of {"nonce", "validator_id", "amount"} that made it into the queue
    (empty if no refill was needed), error is the exception message if proposing stopped early, else None.
    """
    next_nonce, pending_delegations = get_queue_state()
    print(f"Delegation queue: {len(pending_delegations)} pending delegate txs, next nonce {next_nonce}")
    if len(pending_delegations) >= REFILL_BELOW:
        return [], None

    plan = plan_delegations(pending_delegations, REFILL_TO - len(pending_delegations))
    proposed = []
    for i, item in enumerate(plan):
        nonce = next_nonce + i
        print(f"{'[dry run] ' if dry_run else ''}Proposing nonce {nonce}: delegate {item['amount']:,} S to validator {item['validator_id']}")
        if not dry_run:
            try:
                propose_delegation(nonce, item["validator_id"], item["amount"])
            except Exception as e:
                # Stop on the first failure so no nonce gap is left behind
                return proposed, str(e)
        proposed.append({"nonce": nonce, **item})
    return proposed, None


if __name__ == "__main__":
    refill_delegation_queue(dry_run=True)
