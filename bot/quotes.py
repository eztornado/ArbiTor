from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .config import Config


@dataclass(frozen=True)
class Quote:
    amount_out: int
    provider: str


class QuoteClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "token-loop-monitor/0.1"

    def quote(self, input_token: str, output_token: str, amount: int) -> Quote:
        if self.config.chain == "base":
            return self._quote_0x(input_token, output_token, amount)
        return self._quote_jupiter(input_token, output_token, amount)

    def _quote_0x(self, sell: str, buy: str, amount: int) -> Quote:
        if not self.config.zerox_api_key:
            raise RuntimeError("Falta ZEROX_API_KEY para consultar 0x en Base")
        response = self.session.get(
            "https://api.0x.org/swap/allowance-holder/price",
            params={"chainId": 8453, "sellToken": sell, "buyToken": buy,
                    "sellAmount": str(amount)},
            headers={"0x-api-key": self.config.zerox_api_key, "0x-version": "v2"},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        return Quote(amount_out=int(body["buyAmount"]), provider="0x")

    def _quote_jupiter(self, source: str, destination: str, amount: int) -> Quote:
        # Sólo permite HTTPS salvo localhost para pruebas; evita enviar consultas a hosts arbitrarios.
        parsed = urlparse(self.config.jupiter_base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("JUPITER_BASE_URL debe usar HTTPS")
        headers = {"x-api-key": self.config.jupiter_api_key} if self.config.jupiter_api_key else {}
        response = self.session.get(
            f"{self.config.jupiter_base_url}/order",
            params={"inputMint": source, "outputMint": destination, "amount": str(amount)},
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if "outAmount" not in body:
            raise RuntimeError(f"Jupiter no devolvió cotización: {body.get('errorMessage', body)}")
        return Quote(amount_out=int(body["outAmount"]), provider="Jupiter")
