from dataclasses import dataclass
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
    database_path: Path
    zerox_api_key: str
    jupiter_base_url: str
    rpc_url: str
    private_key: str
    wallet_keypair_path: str
    jupiter_api_key: str
    live_trading: bool
    slippage_bps: int
    max_trade_amount: int
    max_daily_loss: int
    max_trades_per_hour: int
    metrics_window: int
    metrics_min_samples: int
    min_win_probability: float
    laya_enabled: bool
    laya_base_url: str
    laya_api_key: str
    laya_block_probability: float


def load_config() -> Config:
    load_dotenv()
    chain = os.getenv("CHAIN", "base").strip().lower()
    if chain not in {"base", "solana"}:
        raise ValueError("CHAIN debe ser 'base' o 'solana'")

    def integer(name: str, default: str) -> int:
        value = int(os.getenv(name, default))
        if value < 0:
            raise ValueError(f"{name} no puede ser negativo")
        return value

    amount = integer("AMOUNT_IN", "0")
    if amount == 0:
        raise ValueError("AMOUNT_IN debe ser mayor que cero")
    quote = os.getenv("QUOTE_TOKEN", "").strip()
    target = os.getenv("TARGET_TOKEN", "").strip()
    if not quote or not target or quote == target:
        raise ValueError("Configura QUOTE_TOKEN y TARGET_TOKEN distintos")
    interval = float(os.getenv("INTERVAL_SECONDS", "20"))
    if interval < 2:
        raise ValueError("INTERVAL_SECONDS debe ser al menos 2")
    win_probability = float(os.getenv("MIN_WIN_PROBABILITY", "0.65"))
    laya_block_probability = float(os.getenv("LAYA_BLOCK_PROBABILITY", "0.70"))
    if not 0 < win_probability < 1 or not 0 < laya_block_probability < 1:
        raise ValueError("Las probabilidades configuradas deben estar entre 0 y 1")
    return Config(
        chain=chain,
        quote_token=quote,
        target_token=target,
        amount_in=amount,
        min_profit_quote=integer("MIN_PROFIT_QUOTE", "0"),
        estimated_cost_quote=integer("ESTIMATED_COST_QUOTE", "0"),
        interval_seconds=interval,
        database_path=Path(os.getenv("DATABASE_PATH", "data/trades.sqlite3")),
        zerox_api_key=os.getenv("ZEROX_API_KEY", "").strip(),
        jupiter_base_url=os.getenv("JUPITER_BASE_URL", "https://api.jup.ag/swap/v2").rstrip("/"),
        rpc_url=os.getenv("RPC_URL", "").strip(),
        private_key=os.getenv("EVM_PRIVATE_KEY", "").strip(),
        wallet_keypair_path=os.getenv("SOLANA_KEYPAIR_PATH", "").strip(),
        jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip(),
        live_trading=os.getenv("LIVE_TRADING", "false").strip().lower() == "true",
        slippage_bps=integer("SLIPPAGE_BPS", "50"),
        max_trade_amount=integer("MAX_TRADE_AMOUNT", "0"),
        max_daily_loss=integer("MAX_DAILY_LOSS", "0"),
        max_trades_per_hour=integer("MAX_TRADES_PER_HOUR", "4"),
        metrics_window=integer("METRICS_WINDOW", "100"),
        metrics_min_samples=integer("METRICS_MIN_SAMPLES", "30"),
        min_win_probability=win_probability,
        laya_enabled=os.getenv("LAYA_ENABLED", "false").strip().lower() == "true",
        laya_base_url=os.getenv("LAYA_BASE_URL", "").strip().rstrip("/"),
        laya_api_key=os.getenv("LAYA_API_KEY", "").strip(),
        laya_block_probability=laya_block_probability,
    )
