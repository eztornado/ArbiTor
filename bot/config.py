from dataclasses import dataclass, replace
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    chain: str
    quote_token: str
    target_token: str
    amount_in: int
    min_profit_quote: int
    estimated_cost_quote: int
    interval_seconds: float
    max_concurrent_quotes: int
    database_path: Path
    zerox_api_key: str
    odos_router_address: str
    jupiter_quote_url: str
    raydium_quote_url: str
    rpc_url: str
    jupiter_api_key: str
    slippage_bps: int
    live_trading: bool
    private_key: str
    solana_keypair_path: str
    max_trade_amount: int
    max_daily_loss: int
    max_trades_per_hour: int
    max_priority_fee_lamports: int
    max_gas_price_gwei: float
    metrics_window: int
    metrics_min_samples: int
    min_win_probability: float
    laya_enabled: bool
    laya_base_url: str
    laya_api_key: str
    laya_block_probability: float
    chains: tuple[str, ...]
    chain_configs: dict[str, dict[str, str]]
    pancake_router_address: str
    thena_router_address: str
    quote_asset: str
    target_asset: str

    def for_chain(self, chain: str) -> "Config":
        market = self.chain_configs[chain]
        return replace(self, chain=chain, quote_token=market["quote_token"],
                       target_token=market["target_token"], rpc_url=market["rpc_url"],
                       odos_router_address=market["odos_router_address"],
                       pancake_router_address=market["pancake_router_address"],
                       thena_router_address=market["thena_router_address"],
                       quote_asset=market["quote_asset"], target_asset=market["target_asset"], chains=(chain,))


def load_config() -> Config:
    load_dotenv()
    raw_chains = os.getenv("CHAINS", "").strip() or os.getenv("CHAIN", "base")
    chains = tuple(dict.fromkeys(part.strip().lower() for part in raw_chains.split(",") if part.strip()))
    if not chains or any(chain not in {"base", "bsc", "solana"} for chain in chains):
        raise ValueError("CHAIN/CHAINS admite base, bsc y solana")
    chain = chains[0]

    def integer(name: str, default: str) -> int:
        value = int(os.getenv(name, default))
        if value < 0:
            raise ValueError(f"{name} no puede ser negativo")
        return value

    amount = integer("AMOUNT_IN", "0")
    if amount == 0:
        raise ValueError("AMOUNT_IN debe ser mayor que cero")
    chain_configs = {}
    for network in chains:
        suffix = network.upper()
        rpc_default = "https://mainnet.base.org" if network == "base" else "https://bsc-dataseed.binance.org" if network == "bsc" else "https://api.mainnet-beta.solana.com"
        if len(chains) > 1:
            quote = os.getenv(f"QUOTE_TOKEN_{suffix}", "").strip()
            target = os.getenv(f"TARGET_TOKEN_{suffix}", "").strip()
            rpc = os.getenv(f"RPC_URL_{suffix}", "").strip()
            quote_asset = os.getenv(f"QUOTE_ASSET_{suffix}", "").strip().upper()
            target_asset = os.getenv(f"TARGET_ASSET_{suffix}", "").strip().upper()
            if not rpc:
                raise ValueError(f"Multichain requiere RPC_URL_{suffix} específico para esa red")
        else:
            quote = os.getenv(f"QUOTE_TOKEN_{suffix}", os.getenv("QUOTE_TOKEN", "")).strip()
            target = os.getenv(f"TARGET_TOKEN_{suffix}", os.getenv("TARGET_TOKEN", "")).strip()
            rpc = os.getenv(f"RPC_URL_{suffix}", os.getenv("RPC_URL", rpc_default)).strip()
            quote_asset = os.getenv(f"QUOTE_ASSET_{suffix}", os.getenv("QUOTE_ASSET", "")).strip().upper()
            target_asset = os.getenv(f"TARGET_ASSET_{suffix}", os.getenv("TARGET_ASSET", "")).strip().upper()
        if not quote or not target or quote.lower() == target.lower():
            raise ValueError(f"Configura QUOTE_TOKEN_{suffix} y TARGET_TOKEN_{suffix} distintos")
        chain_configs[network] = {
            "quote_token": quote,
            "target_token": target,
            "rpc_url": rpc,
            "odos_router_address": os.getenv(f"ODOS_ROUTER_ADDRESS_{suffix}", os.getenv("ODOS_ROUTER_ADDRESS", "")).strip(),
            "pancake_router_address": os.getenv(f"PANCAKE_ROUTER_ADDRESS_{suffix}", "").strip() or (
                "0x8cFe327CEc66d1C090Dd72bd0FF11d690C33a2Eb" if network == "base" else
                "0x10ED43C718714eb63d5aA57B78B54704E256024E" if network == "bsc" else ""),
            "thena_router_address": os.getenv(f"THENA_ROUTER_ADDRESS_{suffix}", os.getenv("THENA_ROUTER_ADDRESS", "")).strip(),
            "quote_asset": quote_asset,
            "target_asset": target_asset,
        }
    if len(chains) > 1:
        if any(not chain_configs[c]["quote_asset"] or not chain_configs[c]["target_asset"] for c in chains):
            raise ValueError("Multichain requiere QUOTE_ASSET_* y TARGET_ASSET_* para declarar activos equivalentes")
        if len({chain_configs[c]["quote_asset"] for c in chains}) != 1 or len({chain_configs[c]["target_asset"] for c in chains}) != 1:
            raise ValueError("Los labels QUOTE_ASSET_* y TARGET_ASSET_* deben ser iguales en las redes comparadas")
    market = chain_configs[chain]
    interval = float(os.getenv("INTERVAL_SECONDS", "2"))
    if interval < 1:
        raise ValueError("INTERVAL_SECONDS debe ser al menos 1")
    max_concurrent_quotes = integer("MAX_CONCURRENT_QUOTES", "12")
    if max_concurrent_quotes < 1:
        raise ValueError("MAX_CONCURRENT_QUOTES debe ser al menos 1")
    win_probability = float(os.getenv("MIN_WIN_PROBABILITY", "0.65"))
    laya_block_probability = float(os.getenv("LAYA_BLOCK_PROBABILITY", "0.70"))
    if not 0 < win_probability < 1 or not 0 < laya_block_probability < 1:
        raise ValueError("Las probabilidades configuradas deben estar entre 0 y 1")
    return Config(
        chain=chain,
        quote_token=market["quote_token"],
        target_token=market["target_token"],
        amount_in=amount,
        min_profit_quote=integer("MIN_PROFIT_QUOTE", "0"),
        estimated_cost_quote=integer("ESTIMATED_COST_QUOTE", "0"),
        interval_seconds=interval,
        max_concurrent_quotes=max_concurrent_quotes,
        database_path=Path(os.getenv("DATABASE_PATH", "data/trades.sqlite3")),
        zerox_api_key=os.getenv("ZEROX_API_KEY", "").strip(),
        odos_router_address=market["odos_router_address"],
        jupiter_quote_url=os.getenv("JUPITER_QUOTE_URL", "https://api.jup.ag/swap/v1/quote").strip(),
        raydium_quote_url=os.getenv("RAYDIUM_QUOTE_URL", "https://transaction-v1.raydium.io/compute/swap-base-in").strip(),
        rpc_url=market["rpc_url"],
        jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip(),
        slippage_bps=integer("SLIPPAGE_BPS", "50"),
        live_trading=os.getenv("LIVE_TRADING", "false").strip().lower() == "true",
        private_key=os.getenv("EVM_PRIVATE_KEY", "").strip(),
        solana_keypair_path=os.getenv("SOLANA_KEYPAIR_PATH", "").strip(),
        max_trade_amount=integer("MAX_TRADE_AMOUNT", "0"),
        max_daily_loss=integer("MAX_DAILY_LOSS", "0"),
        max_trades_per_hour=integer("MAX_TRADES_PER_HOUR", "1000"),
        max_priority_fee_lamports=integer("MAX_PRIORITY_FEE_LAMPORTS", "1000000"),
        max_gas_price_gwei=float(os.getenv("MAX_GAS_PRICE_GWEI", "5")),
        metrics_window=integer("METRICS_WINDOW", "100"),
        metrics_min_samples=integer("METRICS_MIN_SAMPLES", "30"),
        min_win_probability=win_probability,
        laya_enabled=os.getenv("LAYA_ENABLED", "false").strip().lower() == "true",
        laya_base_url=os.getenv("LAYA_BASE_URL", "").strip().rstrip("/"),
        laya_api_key=os.getenv("LAYA_API_KEY", "").strip(),
        laya_block_probability=laya_block_probability,
        chains=chains,
        chain_configs=chain_configs,
        pancake_router_address=market["pancake_router_address"],
        thena_router_address=market["thena_router_address"],
        quote_asset=market["quote_asset"],
        target_asset=market["target_asset"],
    )
