"""Cross-aggregator, same-chain arbitrage opportunity scanner (quote-only)."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .quotes import Quote, QuoteClient


@dataclass(frozen=True)
class Opportunity:
    buy_provider: str
    sell_provider: str
    amount_in: int
    target_amount: int
    amount_out: int
    gross_edge: int
    net_edge: int
    buy_chain: str = ""
    sell_chain: str = ""


def find_best_opportunity(client: QuoteClient, quote_token: str, target_token: str,
                          amount_in: int, estimated_cost: int) -> tuple[Opportunity | None, list[str]]:
    """Compare independent venue routes in parallel and return the best net edge.

    A route pair must use distinct aggregators. With only one available provider,
    no arbitrage candidate is produced (a round trip on one aggregator is not arb).
    """
    providers = client.providers()
    if len(providers) < 2:
        return None, providers

    buy_quotes = client.quotes(quote_token, target_token, amount_in)
    possible: list[Opportunity] = []
    errors = []
    with ThreadPoolExecutor(max_workers=client.config.max_concurrent_quotes) as pool:
        futures = {}
        for buy in buy_quotes:
            for sell_provider in providers:
                if sell_provider == buy.provider:
                    continue
                future = pool.submit(client.quote_from, sell_provider, target_token,
                                     quote_token, buy.amount_out)
                futures[future] = (buy, sell_provider)
        for future in as_completed(futures):
            buy, sell_provider = futures[future]
            try:
                sell = future.result()
                gross = sell.amount_out - amount_in
                possible.append(Opportunity(buy.provider, sell.provider, amount_in,
                                            buy.amount_out, sell.amount_out, gross,
                                            gross - estimated_cost,
                                            client.config.chain, client.config.chain))
            except Exception as exc:
                errors.append(f"{buy.provider}->{sell_provider}: {exc}")
    if not possible:
        return None, errors
    return max(possible, key=lambda item: item.net_edge), errors


def find_best_multichain_opportunity(clients: dict[str, QuoteClient],
                                     quote_tokens: dict[str, str],
                                     target_tokens: dict[str, str], amount_in: int,
                                     estimated_cost: int) -> tuple[Opportunity | None, list[str]]:
    """Compare pre-funded markets with matching token denominations across chains."""
    decimals = {}
    for chain, client in clients.items():
        if client.market_decimals is None:
            quote_decimals = client.token_decimals(quote_tokens[chain])
            target_decimals = client.token_decimals(target_tokens[chain])
            client.market_decimals = (quote_decimals, target_decimals)
        decimals[chain] = client.market_decimals
    if len({pair[0] for pair in decimals.values()}) != 1 or len({pair[1] for pair in decimals.values()}) != 1:
        raise ValueError("Multichain detenido: los tokens equivalentes deben tener los mismos decimales en todas las redes")
    buys = []
    errors = []
    for chain, client in clients.items():
        quotes = client.quotes(quote_tokens[chain], target_tokens[chain], amount_in)
        buys.extend((chain, quote) for quote in quotes)
    possible = []
    futures = {}
    with ThreadPoolExecutor(max_workers=max(1, max(c.config.max_concurrent_quotes for c in clients.values()))) as pool:
        for buy_chain, buy in buys:
            for sell_chain, sell_client in clients.items():
                for sell_provider in sell_client.providers():
                    if sell_chain == buy_chain and sell_provider == buy.provider:
                        continue
                    future = pool.submit(
                        sell_client.quote_from, sell_provider, target_tokens[sell_chain],
                        quote_tokens[sell_chain], buy.amount_out)
                    futures[future] = (buy_chain, buy, sell_chain, sell_provider)
        for future in as_completed(futures):
            buy_chain, buy, sell_chain, sell_provider = futures[future]
            try:
                sell = future.result()
                gross = sell.amount_out - amount_in
                possible.append(Opportunity(buy.provider, sell.provider, amount_in,
                    buy.amount_out, sell.amount_out, gross, gross - estimated_cost,
                    buy_chain, sell_chain))
            except Exception as exc:
                errors.append(f"{buy_chain}:{buy.provider}->{sell_chain}:{sell_provider}: {exc}")
    if not possible:
        return None, errors
    return max(possible, key=lambda item: item.net_edge), errors
