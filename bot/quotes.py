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
V2_ROUTER_ABI = [
    {"type": "function", "name": "getAmountsOut", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"type": "function", "name": "swapExactTokensForTokens", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]
THENA_ROUTER_ABI = [
    {"type": "function", "name": "factory", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "getAmountsOut", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "routes", "type": "tuple[]", "components": [
                    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                    {"name": "stable", "type": "bool"}]}],
     "outputs": [{"type": "uint256[]"}]},
    {"type": "function", "name": "swapExactTokensForTokens", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "routes", "type": "tuple[]", "components": [
                    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                    {"name": "stable", "type": "bool"}]},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "outputs": [{"type": "uint256[]"}]},
]
CHAIN_IDS = {"base": 8453, "bsc": 56}
TOKEN_DECIMALS_ABI = [{"type": "function", "name": "decimals", "stateMutability": "view",
                      "inputs": [], "outputs": [{"type": "uint8"}]}]
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
        self.market_decimals = None

    def token_decimals(self, token: str) -> int:
        """Return raw-unit precision for an EVM token or Solana mint."""
        if self.config.chain in CHAIN_IDS:
            w3 = self._evm_web3()
            contract = w3.eth.contract(address=w3.to_checksum_address(token),
                                       abi=TOKEN_DECIMALS_ABI)
            return int(contract.functions.decimals().call())
        parsed = urlparse(self.config.rpc_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("RPC_URL_SOLANA debe usar HTTPS salvo localhost")
        response = self._session().post(
            self.config.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply",
                  "params": [token, {"commitment": "confirmed"}]},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error") or not (body.get("result") or {}).get("value"):
            raise RuntimeError(f"No se pudieron leer los decimales del mint {token}: {body.get('error', body)}")
        return int(body["result"]["value"]["decimals"])

    def _session(self) -> requests.Session:
        # Keep connection pools isolated per worker thread.
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
            self._local.session.headers["User-Agent"] = "token-arbitrage-scanner/0.2"
        return self._local.session

    def providers(self) -> list[str]:
        if self.config.chain == "base":
            return ["0x", "Odos", "Aerodrome", "PancakeSwap V2"]
        if self.config.chain == "bsc":
            providers = ["0x", "Odos", "PancakeSwap V2"]
            if self.config.thena_router_address:
                providers.append("THENA Classic")
            return providers
        return ["Raydium", "Orca", "Meteora", "Pump.fun", "Stonk.fun"]

    def quote_from(self, provider: str, input_token: str, output_token: str, amount: int) -> Quote:
        if provider == "0x":
            return self._quote_0x(input_token, output_token, amount)
        if provider == "Odos":
            return self._quote_odos(input_token, output_token, amount)
        if provider == "Aerodrome":
            return self._quote_aerodrome(input_token, output_token, amount)
        if provider == "PancakeSwap V2":
            return self._quote_pancake(input_token, output_token, amount)
        if provider == "THENA Classic":
            return self._quote_thena(input_token, output_token, amount)
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
            params={"chainId": CHAIN_IDS[self.config.chain], "sellToken": sell, "buyToken": buy,
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
            json={"chainId": CHAIN_IDS[self.config.chain],
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
        if self.config.chain != "base":
            raise RuntimeError("Aerodrome solo está habilitado para Base")
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
        factory = router.functions.factory().call()
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

    def _evm_web3(self):
        from web3 import Web3
        if not hasattr(self._local, "evm_web3"):
            parsed = urlparse(self.config.rpc_url)
            if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("RPC_URL debe usar HTTPS salvo localhost")
            w3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": 12}))
            if not w3.is_connected() or w3.eth.chain_id != CHAIN_IDS[self.config.chain]:
                raise RuntimeError(f"RPC_URL no corresponde a {self.config.chain} (chainId {CHAIN_IDS[self.config.chain]})")
            self._local.evm_web3 = w3
        return self._local.evm_web3

    def _quote_pancake(self, sell: str, buy: str, amount: int) -> Quote:
        if self.config.chain not in {"base", "bsc"} or not self.config.pancake_router_address:
            raise RuntimeError("PancakeSwap V2 no está configurado para esta cadena")
        w3 = self._evm_web3()
        router = w3.eth.contract(address=w3.to_checksum_address(self.config.pancake_router_address),
                                 abi=V2_ROUTER_ABI)
        path = [w3.to_checksum_address(sell), w3.to_checksum_address(buy)]
        amounts = router.functions.getAmountsOut(amount, path).call()
        return Quote(amount_out=int(amounts[-1]), provider="PancakeSwap V2")

    def _quote_thena(self, sell: str, buy: str, amount: int) -> Quote:
        if self.config.chain != "bsc" or not self.config.thena_router_address:
            raise RuntimeError("THENA Classic requiere BNB Chain y THENA_ROUTER_ADDRESS")
        w3 = self._evm_web3()
        router = w3.eth.contract(address=w3.to_checksum_address(self.config.thena_router_address),
                                 abi=THENA_ROUTER_ABI)
        # THENA Classic's RouterV2 exposes factory(); defaultFactory() belongs
        # to other Solidly forks and is not part of this router's interface.
        router.functions.factory().call()
        best = 0
        for stable in (False, True):
            route = [(w3.to_checksum_address(sell), w3.to_checksum_address(buy), stable)]
            try:
                best = max(best, int(router.functions.getAmountsOut(amount, route).call()[-1]))
            except Exception:
                continue
        if best <= 0:
            raise RuntimeError("THENA Classic no encontró pool directo con liquidez")
        return Quote(amount_out=best, provider="THENA Classic")

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
