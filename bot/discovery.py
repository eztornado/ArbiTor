"""Discover token/pool candidates across supported indexer APIs.

Discovery is informational only. It never edits .env or enables trading.
"""
from dataclasses import asdict, dataclass, field
import os
import re
from typing import Any

import requests
from dotenv import load_dotenv


SUPPORTED_TRADING_CHAINS = {"base", "bsc", "solana"}


@dataclass
class Candidate:
    chain: str
    token_address: str
    token_symbol: str
    token_name: str
    dex: str
    pair_address: str
    counter_token_address: str = ""
    counter_token_symbol: str = ""
    counter_token_name: str = ""
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    transactions_24h: int | None = None
    price_usd: float | None = None
    price_change_h1_pct: float | None = None
    pair_url: str = ""
    sources: list[str] = field(default_factory=list)
    coingecko_coin_id: str = ""

    @property
    def execution_supported(self) -> bool:
        return self.chain in SUPPORTED_TRADING_CHAINS

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["execution_supported"] = self.execution_supported
        return value


@dataclass
class RankedToken:
    chain: str
    token_address: str
    token_symbol: str
    token_name: str
    coingecko_coin_id: str
    pools: list[Candidate]
    liquidity_usd: float
    volume_24h_usd: float
    transactions_24h: int
    dexes: list[str]
    sources: list[str]
    crosschain_count: int
    score: int
    reasons: list[str]


def rank_candidates(candidates: list[Candidate]) -> list[RankedToken]:
    """Rank discovery candidates using explainable market-quality indicators."""
    grouped: dict[tuple[str, str], list[Candidate]] = {}
    for item in candidates:
        grouped.setdefault((item.chain, item.token_address.casefold()), []).append(item)
    provisional = []
    for (chain, _address), pools in grouped.items():
        representative = max(pools, key=lambda p: p.liquidity_usd or 0)
        # Indexer results for the same pool are merged before this step.
        liquidity = sum(pool.liquidity_usd or 0 for pool in pools)
        volume = sum(pool.volume_24h_usd or 0 for pool in pools)
        transactions = sum(pool.transactions_24h or 0 for pool in pools)
        dexes = sorted({pool.dex for pool in pools if pool.dex})
        sources = sorted({source for pool in pools for source in pool.sources})
        coin_ids = {pool.coingecko_coin_id for pool in pools if pool.coingecko_coin_id}
        coin_id = next(iter(coin_ids)) if len(coin_ids) == 1 else ""
        provisional.append({
            "chain": chain, "token_address": representative.token_address,
            "token_symbol": representative.token_symbol, "token_name": representative.token_name,
            "coingecko_coin_id": coin_id, "pools": pools, "liquidity": liquidity,
            "volume": volume, "transactions": transactions, "dexes": dexes, "sources": sources,
        })
    by_coin_id: dict[str, set[str]] = {}
    for item in provisional:
        if item["coingecko_coin_id"]:
            by_coin_id.setdefault(item["coingecko_coin_id"], set()).add(item["chain"])

    def log_score(value: float, ceiling: float) -> float:
        import math
        return min(100.0, 100.0 * math.log1p(max(0.0, value)) / math.log1p(ceiling))

    ranked = []
    for item in provisional:
        crosschain = len(by_coin_id.get(item["coingecko_coin_id"], {item["chain"]}))
        liquidity_score = log_score(item["liquidity"], 250_000)
        volume_score = log_score(item["volume"], 500_000)
        activity_score = log_score(item["transactions"], 2_000)
        dex_score = min(100.0, len(item["dexes"]) * 25.0)
        source_score = 100.0 if {"dexscreener", "coingecko"}.issubset(item["sources"]) else 25.0
        chain_score = 100.0 if crosschain >= 2 else 0.0
        score = round(0.35 * liquidity_score + 0.25 * volume_score +
                      0.15 * activity_score + 0.10 * dex_score +
                      0.10 * chain_score + 0.05 * source_score)
        reasons = [f"liquidez ${item['liquidity']:,.0f}",
                   f"volumen 24h ${item['volume']:,.0f}",
                   f"{item['transactions']} transacciones 24h",
                   f"{len(item['dexes'])} DEX/pools indexados"]
        if crosschain >= 2:
            reasons.append(f"mismo CoinGecko ID encontrado en {crosschain} redes")
        else:
            reasons.append("sin identidad multichain corroborada por CoinGecko")
        ranked.append(RankedToken(
            chain=item["chain"], token_address=item["token_address"],
            token_symbol=item["token_symbol"], token_name=item["token_name"],
            coingecko_coin_id=item["coingecko_coin_id"], pools=item["pools"],
            liquidity_usd=item["liquidity"], volume_24h_usd=item["volume"],
            transactions_24h=item["transactions"], dexes=item["dexes"],
            sources=item["sources"], crosschain_count=crosschain,
            score=score, reasons=reasons,
        ))
    return sorted(ranked, key=lambda item: (item.score, item.liquidity_usd), reverse=True)


class TokenDiscovery:
    """Search DEX Screener and optionally CoinGecko Onchain for matching pools."""

    def __init__(self, coingecko_api_key: str | None = None, timeout: float = 20):
        load_dotenv()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "token-arbitrage-discovery/0.3"
        self.coingecko_api_key = (coingecko_api_key or os.getenv("COINGECKO_PRO_API_KEY", "")).strip()
        self.coingecko_pages = max(1, min(10, int(os.getenv("COINGECKO_DISCOVERY_PAGES", "3"))))
        self.timeout = timeout

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_address_query(query: str) -> bool:
        return query.startswith("0x") or (len(query) >= 32 and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", query) is not None)

    @staticmethod
    def _matches(query: str, name: str, symbol: str, address: str) -> bool:
        q = query.strip().casefold()
        if not q:
            return False
        if TokenDiscovery._is_address_query(query):
            return address.casefold() == q
        return symbol.casefold() == q or q in name.casefold()

    def _dexscreener(self, query: str) -> list[Candidate]:
        response = self.session.get(
            "https://api.dexscreener.com/latest/dex/search", params={"q": query}, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        candidates = []
        for pair in body.get("pairs") or []:
            chain = str(pair.get("chainId") or "").lower()
            base = pair.get("baseToken") or {}
            quote = pair.get("quoteToken") or {}
            for token in (base, quote):
                address = str(token.get("address") or "")
                symbol = str(token.get("symbol") or "")
                name = str(token.get("name") or "")
                if not address or not self._matches(query, name, symbol, address):
                    continue
                is_base = token is base
                counter = quote if is_base else base
                price = self._number(pair.get("priceUsd"))
                if not is_base:
                    # DEX Screener's pair priceUsd describes the base token.
                    # Do not infer the quote token's USD price from priceNative.
                    price = None
                liquidity = pair.get("liquidity") or {}
                volume = pair.get("volume") or {}
                transactions = pair.get("txns") or {}
                h24_transactions = transactions.get("h24") or {}
                candidates.append(Candidate(
                    chain=chain, token_address=address, token_symbol=symbol, token_name=name,
                    dex=str(pair.get("dexId") or "unknown"),
                    pair_address=str(pair.get("pairAddress") or ""),
                    counter_token_address=str(counter.get("address") or ""),
                    counter_token_symbol=str(counter.get("symbol") or ""),
                    counter_token_name=str(counter.get("name") or ""),
                    liquidity_usd=self._number(liquidity.get("usd")),
                    volume_24h_usd=self._number(volume.get("h24")),
                    transactions_24h=(int(h24_transactions.get("buys", 0) or 0) +
                                      int(h24_transactions.get("sells", 0) or 0)),
                    price_usd=price,
                    price_change_h1_pct=self._number((pair.get("priceChange") or {}).get("h1")) if is_base else None,
                    pair_url=str(pair.get("url") or ""),
                    sources=["dexscreener"],
                ))
        return candidates

    def _coingecko(self, query: str) -> list[Candidate]:
        if not self.coingecko_api_key:
            return []
        candidates = []
        for page in range(1, self.coingecko_pages + 1):
            response = self.session.get(
                "https://pro-api.coingecko.com/api/v3/onchain/search/pools",
                params={"query": query, "include": "base_token,quote_token,dex", "page": page},
                headers={"x-cg-pro-api-key": self.coingecko_api_key}, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            included = {item.get("id"): item for item in body.get("included") or []}
            pools = body.get("data") or []
            if not pools:
                break
            for pool in pools:
                attrs = pool.get("attributes") or {}
                relationships = pool.get("relationships") or {}
                base_ref = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
                quote_ref = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
                dex_ref = ((relationships.get("dex") or {}).get("data") or {}).get("id")
                network_ref = ((relationships.get("network") or {}).get("data") or {}).get("id")
                network = str(network_ref or str(pool.get("id") or "").split("_", 1)[0]).lower()
                for token_ref, counter_ref, price_field in (
                        (base_ref, quote_ref, "base_token_price_usd"),
                        (quote_ref, base_ref, "quote_token_price_usd")):
                    token = included.get(token_ref) or {}
                    counter = included.get(counter_ref) or {}
                    token_attrs = token.get("attributes") or {}
                    address = str(token_attrs.get("address") or "")
                    symbol = str(token_attrs.get("symbol") or "")
                    name = str(token_attrs.get("name") or "")
                    if not address or not self._matches(query, name, symbol, address):
                        continue
                    candidates.append(Candidate(
                        chain=network, token_address=address, token_symbol=symbol, token_name=name,
                        dex=str(((included.get(dex_ref) or {}).get("attributes") or {}).get("name")
                                or dex_ref or "unknown"),
                        pair_address=str(attrs.get("address") or ""),
                        counter_token_address=str((counter.get("attributes") or {}).get("address") or ""),
                        counter_token_symbol=str((counter.get("attributes") or {}).get("symbol") or ""),
                        counter_token_name=str((counter.get("attributes") or {}).get("name") or ""),
                        liquidity_usd=self._number(attrs.get("reserve_in_usd")),
                        volume_24h_usd=self._number((attrs.get("volume_usd") or {}).get("h24")),
                        transactions_24h=(int(((attrs.get("transactions") or {}).get("h24") or {}).get("buys", 0) or 0) +
                                          int(((attrs.get("transactions") or {}).get("h24") or {}).get("sells", 0) or 0)),
                        price_usd=self._number(attrs.get(price_field)),
                        price_change_h1_pct=self._number((attrs.get("price_change_percentage") or {}).get("h1")) if token_ref == base_ref else None,
                        pair_url="", sources=["coingecko"],
                        coingecko_coin_id=str(token_attrs.get("coingecko_coin_id") or ""),
                    ))
        return candidates

    def discover(self, query: str) -> tuple[list[Candidate], list[str]]:
        query = query.strip()
        if not query:
            raise ValueError("Indica un símbolo, nombre o address/mint para descubrir")
        candidates: list[Candidate] = []
        errors: list[str] = []
        for source, search in (("dexscreener", self._dexscreener), ("coingecko", self._coingecko)):
            if source == "coingecko" and not self.coingecko_api_key:
                continue
            try:
                candidates.extend(search(query))
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        # Merge the same chain/token/pool found by both providers.
        merged: dict[tuple[str, str, str], Candidate] = {}
        for candidate in candidates:
            key = (candidate.chain, candidate.token_address.casefold(), candidate.pair_address.casefold())
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
                continue
            existing.sources = sorted(set(existing.sources + candidate.sources))
            if not existing.pair_url:
                existing.pair_url = candidate.pair_url
            if not existing.coingecko_coin_id:
                existing.coingecko_coin_id = candidate.coingecko_coin_id
            if existing.liquidity_usd is None:
                existing.liquidity_usd = candidate.liquidity_usd
            if existing.volume_24h_usd is None:
                existing.volume_24h_usd = candidate.volume_24h_usd
            if existing.price_usd is None:
                existing.price_usd = candidate.price_usd
        ordered = list(merged.values())
        ordered.sort(key=lambda item: (item.liquidity_usd or 0, item.volume_24h_usd or 0), reverse=True)
        return ordered, errors
