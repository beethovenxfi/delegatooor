# peg_monitor.py
# Compare stS market rate (KyberSwap quote) against stS redemption rate (contract getRate()).
import os
import requests
from web3 import Web3

STS_ADDRESS = "0xE5DA20F15420aD15DE0fa650600aFc998bbE3955"
NATIVE_S_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"  # Kyber native-token sentinel (lowercase required)
KYBER_ROUTES_URL = "https://aggregator-api.kyberswap.com/sonic/api/v1/routes"
QUOTE_AMOUNT_S = 100_000  # size of the buy used to sample the market rate
PEG_ALERT_DISCOUNT = 0.003  # alert when market rate is >= 0.3% below redemption rate
PEG_ALERT_DISCORD_ID = 232514597200855040  # ardo
REQUEST_TIMEOUT = 10

_STS_ABI = [
    {"name": "getRate", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]


def get_redemption_rate() -> float:
    """S per stS according to the stS contract (getRate, 18 decimals)."""
    w3 = Web3(Web3.HTTPProvider(os.getenv("SONIC_RPC_URL")))
    contract = w3.eth.contract(address=Web3.to_checksum_address(STS_ADDRESS), abi=_STS_ABI)
    return contract.functions.getRate().call() / 1e18


def get_market_rate(amount_s: float = QUOTE_AMOUNT_S) -> float:
    """S per stS when buying stS with `amount_s` S on the open market (KyberSwap aggregator)."""
    params = {
        "tokenIn": NATIVE_S_ADDRESS,
        "tokenOut": STS_ADDRESS,
        "amountIn": str(int(amount_s * 10**18)),
    }
    resp = requests.get(KYBER_ROUTES_URL, params=params, headers={"x-client-id": "delegatooooor"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Kyber error {body.get('code')}: {body.get('message')}")
    amount_out = int(body["data"]["routeSummary"]["amountOut"]) / 1e18
    if amount_out <= 0:
        raise RuntimeError("Kyber returned zero stS out")
    return amount_s / amount_out


def check_peg() -> dict:
    """
    Returns {"redemption_rate", "market_rate", "discount", "alert"}.
    discount = how far market rate sits below redemption rate (positive = stS trading at a discount).
    """
    redemption_rate = get_redemption_rate()
    market_rate = get_market_rate()
    discount = 1 - market_rate / redemption_rate
    return {
        "redemption_rate": redemption_rate,
        "market_rate": market_rate,
        "discount": discount,
        "alert": discount >= PEG_ALERT_DISCOUNT,
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
        return f"**stS/S Rate:** unavailable ({e})", False

    lines = [
        "**stS/S Rate** (S per stS):",
        f"- Redemption (contract): `{peg['redemption_rate']:.6f}`",
        f"- Market (Kyber, {QUOTE_AMOUNT_S:,} S buy): `{peg['market_rate']:.6f}`",
        f"- Market discount: `{peg['discount'] * 100:+.3f}%`",
    ]
    if peg["alert"]:
        lines.append(
            f"🚨 **stS trading {peg['discount'] * 100:.3f}% below redemption rate** "
            f"(threshold {PEG_ALERT_DISCOUNT * 100:.1f}%). Buy stS off the market and redeem for S."
        )
        if ping:
            lines.append(f"<@{PEG_ALERT_DISCORD_ID}> peg arb opportunity.")
    return "\n".join(lines), peg["alert"]
