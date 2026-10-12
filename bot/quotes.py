from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import threading
from urllib.parse import urlparse

import requests

from .config import Config

log = logging.getLogger(__name__)
AERODROME_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
AERODROME_ROUTER_ABI = [
    {"type": "function", "name": "defaultFactory", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "getAmountsOut", "stateMutability": "view",
     "inputs": [
         {"name": "amountIn", "type": "uint256"},
         {"name": "routes", "type": "tuple[]", "components": [
             {"name": "from", "type": "address"},
             {"name": "to", "type": "address"},
             {"name": "stable", "type": "bool"},
             {"name": "factory", "type": "address"},
         ]},
     ], "outputs": [{"type": "uint256[]"}]},
    {"type": "function", "name": "swapExactTokensForTokens", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "amountIn", "type": "uint256"},
         {"name": "amountOutMin", "type": "uint256"},
         {"name": "routes", "type": "tuple[]", "components": [
             {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
             {"name": "stable", "type": "bool"}, {"name": "factory", "type": "address"},
         ]},
         {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"},
     ], "outputs": [{"type": "uint256[]"}]},
]
DEXES = {
    "Orca": ["Orca V1", "Orca V2", "Whirlpool"],
    "Meteora": ["Meteora", "Meteora DLMM", "Meteora DAMM v2"],
    "Pump.fun": ["Pump.fun", "Pump.fun Amm"],
    "Stonk.fun": ["Raydium Launchlab"],
}


@dataclass(frozen=True)
class Quote:
    amount_out: int
    provider: str


class QuoteClient:
    def __init__(self, config: Config):
        self.config = config
        self._local = threading.local()

    def _session(self) -> requests.Session:
        # Keep connection pools isolated per worker thread.
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
            self._local.session.headers["User-Agent"] = "token-arbitrage-scanner/0.2"
        return self._local.session

    def providers(self) -> list[str]:
        # Aerodrome is queried directly from its router; aggregators may also route there.
        if self.config.chain == "base":
            return ["0x", "Odos", "Aerodrome"]
        return ["Raydium", "Orca", "Meteora", "Pump.fun", "Stonk.fun"]

    def quote_from(self, provider: str, input_token: str, output_token: str, amount: int) -> Quote:
        if provider == "0x":
            return self._quote_0x(input_token, output_token, amount)
        if provider == "Odos":
            return self._quote_odos(input_token, output_token, amount)
        if provider == "Aerodrome":
            return self._quote_aerodrome(input_token, output_token, amount)
        if provider == "Raydium":
            return self._quote_raydium(input_token, output_token, amount)
        if provider in {"Orca", "Meteora", "Pump.fun", "Stonk.fun"}:
            return self._quote_jupiter_dex(provider, DEXES[provider], input_token, output_token, amount)
        raise ValueError(f"Proveedor desconocido: {provider}")

    def quote(self, input_token: str, output_token: str, amount: int) -> Quote:
        quotes = self.quotes(input_token, output_token, amount)
        if not quotes:
            raise RuntimeError("Ningún proveedor devolvió una cotización válida")
        return max(quotes, key=lambda quote: quote.amount_out)

    def quotes(self, input_token: str, output_token: str, amount: int) -> list[Quote]:
        results = []
        with ThreadPoolExecutor(max_workers=min(self.config.max_concurrent_quotes, len(self.providers()))) as pool:
            futures = {pool.submit(self.quote_from, provider, input_token, output_token, amount): provider
                       for provider in self.providers()}
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    log.warning("Cotización %s falló: %s", provider, exc)
        return results

    def _quote_0x(self, sell: str, buy: str, amount: int) -> Quote:
        if not self.config.zerox_api_key:
            raise RuntimeError("Falta ZEROX_API_KEY para consultar 0x en Base")
        response = self._session().get(
            "https://api.0x.org/swap/allowance-holder/price",
            params={"chainId": 8453, "sellToken": sell, "buyToken": buy,
                    "sellAmount": str(amount)},
            headers={"0x-api-key": self.config.zerox_api_key, "0x-version": "v2"},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        return Quote(amount_out=int(body["buyAmount"]), provider="0x")

    def _quote_odos(self, sell: str, buy: str, amount: int) -> Quote:
        response = self._session().post(
            "https://api.odos.xyz/sor/quote/v2",
            json={"chainId": 8453,
                  "inputTokens": [{"tokenAddress": sell, "amount": str(amount)}],
                  "outputTokens": [{"tokenAddress": buy, "proportion": 1}],
                  "slippageLimitPercent": self.config.slippage_bps / 100,
                  "compact": True},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        outputs = body.get("outAmounts") or []
        if not outputs:
            raise RuntimeError(f"Odos no devolvió outAmounts: {body}")
        return Quote(amount_out=int(outputs[0]), provider="Odos")

    def _quote_aerodrome(self, sell: str, buy: str, amount: int) -> Quote:
        parsed = urlparse(self.config.rpc_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("RPC_URL debe usar HTTPS salvo en localhost")
        if not hasattr(self._local, "web3"):
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": 12}))
            if not w3.is_connected() or w3.eth.chain_id != 8453:
                raise RuntimeError("RPC_URL no responde o no corresponde a Base (chainId 8453)")
            self._local.web3 = w3
            self._local.aerodrome = w3.eth.contract(
                address=Web3.to_checksum_address(AERODROME_ROUTER), abi=AERODROME_ROUTER_ABI)
        router = self._local.aerodrome
        factory = router.functions.defaultFactory().call()
        best = 0
        for stable in (False, True):
            route = [(sell, buy, stable, factory)]
            try:
                amounts = router.functions.getAmountsOut(amount, route).call()
                best = max(best, int(amounts[-1]))
            except Exception:
                continue
        if best <= 0:
            raise RuntimeError("Aerodrome no encontró pool directo con liquidez para el par")
        return Quote(amount_out=best, provider="Aerodrome")

    def _quote_jupiter_dex(self, provider: str, dexes: list[str], source: str,
                           destination: str, amount: int) -> Quote:
        parsed = urlparse(self.config.jupiter_quote_url)
        if parsed.scheme != "https":
            raise ValueError("JUPITER_QUOTE_URL debe usar HTTPS")
        if not self.config.jupiter_api_key:
            raise RuntimeError("Las cotizaciones filtradas por DEX requieren JUPITER_API_KEY")
        response = self._session().get(
            self.config.jupiter_quote_url,
            params={"inputMint": source, "outputMint": destination, "amount": str(amount),
                    "slippageBps": str(self.config.slippage_bps), "dexes": ",".join(dexes)},
            headers={"x-api-key": self.config.jupiter_api_key}, timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("outAmount"):
            raise RuntimeError(f"Jupiter no encontró ruta {provider}: {body.get('error', body)}")
        labels = {step.get("swapInfo", {}).get("label")
                  for step in (body.get("routePlan") or [])}
        if not labels or not labels.issubset(set(dexes)):
            raise RuntimeError(f"La ruta de Jupiter no coincide con el DEX solicitado ({provider}): {labels}")
        return Quote(amount_out=int(body["outAmount"]), provider=provider)

    def _quote_raydium(self, source: str, destination: str, amount: int) -> Quote:
        parsed = urlparse(self.config.raydium_quote_url)
        if parsed.scheme != "https":
            raise ValueError("RAYDIUM_QUOTE_URL debe usar HTTPS")
        response = self._session().get(
            self.config.raydium_quote_url,
            params={"inputMint": source, "outputMint": destination, "amount": str(amount),
                    "slippageBps": str(self.config.slippage_bps), "txVersion": "V0"},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or {}
        if body.get("success") is False or not data.get("outputAmount"):
            raise RuntimeError(f"Raydium no encontró ruta: {body.get('msg', body)}")
        return Quote(amount_out=int(data["outputAmount"]), provider="Raydium")
