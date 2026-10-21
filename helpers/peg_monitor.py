# peg_monitor.py
# stS peg, computed the same way as defiwars.xyz/wars/sonic ("Peg for 10k"):
#   expected ratio = stS contract getRate()                (S per stS, redemption)
#   market ratio   = S received selling PEG_AMOUNT_STS stS / PEG_AMOUNT_STS (S per stS, KyberSwap best route)
#   peg            = market ratio / expected ratio
import os
import requests
from web3 import Web3

STS_ADDRESS = "0xE5DA20F15420aD15DE0fa650600aFc998bbE3955"
NATIVE_S_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"  # Kyber native-token sentinel (lowercase required)
KYBER_ROUTES_URL = "https://aggregator-api.kyberswap.com/sonic/api/v1/routes"
PEG_AMOUNT_STS = 100_000  # stS sold to sample the market ratio (defiwars uses 10k; we size for our real op)
PEG_ALERT_THRESHOLD = 0.997  # alert when peg < 99.7%
PEG_ALERT_DISCORD_ID = 232514597200855040  # ardo
REQUEST_TIMEOUT = 10

_STS_ABI = [
    {"name": "getRate", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]


def get_expected_ratio() -> float:
    """S per stS according to the stS contract (getRate, 18 decimals)."""
    w3 = Web3(Web3.HTTPProvider(os.getenv("SONIC_RPC_URL")))
    contract = w3.eth.contract(address=Web3.to_checksum_address(STS_ADDRESS), abi=_STS_ABI)
    return contract.functions.getRate().call() / 1e18


def get_market_ratio(amount_sts: float = PEG_AMOUNT_STS) -> float:
    """S per stS when selling `amount_sts` stS for native S on KyberSwap (best route)."""
    params = {
        "tokenIn": STS_ADDRESS,
        "tokenOut": NATIVE_S_ADDRESS,
        "amountIn": str(int(amount_sts * 10**18)),
    }
    resp = requests.get(KYBER_ROUTES_URL, params=params, headers={"x-client-id": "delegatooooor"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Kyber error {body.get('code')}: {body.get('message')}")
    amount_out = int(body["data"]["routeSummary"]["amountOut"]) / 1e18
    return amount_out / amount_sts


def check_peg() -> dict:
    """Returns {"expected_ratio", "market_ratio", "peg", "alert"}. peg = market_ratio / expected_ratio."""
    expected_ratio = get_expected_ratio()
    market_ratio = get_market_ratio()
    peg = market_ratio / expected_ratio
    return {
        "expected_ratio": expected_ratio,
        "market_ratio": market_ratio,
        "peg": peg,
        "alert": peg < PEG_ALERT_THRESHOLD,
    }


def build_peg_section(ping: bool = True) -> tuple[str, bool]:
    """
    Build the report section. Returns (text, alert_triggered).
    Never raises: on failure the section says so and alert_triggered is False.
    """
    try:
        peg = check_peg()
    except Exception as e:
        print(f"Peg check failed: {e}")
        return f"**stS Peg:** unavailable ({e})", False

    lines = [
        "**stS Peg** (via KyberSwap):",
        f"- Expected ratio: `{peg['expected_ratio']:.5f}`",
        f"- Market ratio: `{peg['market_ratio']:.5f}`",
        f"- Peg for {PEG_AMOUNT_STS // 1000}k: `{peg['peg'] * 100:.2f}%`",
    ]
    if peg["alert"]:
        lines.append(
            f"🚨 **stS peg below {PEG_ALERT_THRESHOLD * 100:.1f}%.** "
            f"Buy stS off the market and redeem for S via contract."
        )
        if ping:
            lines.append(f"<@{PEG_ALERT_DISCORD_ID}>")
    return "\n".join(lines), peg["alert"]
