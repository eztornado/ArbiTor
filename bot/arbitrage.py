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
                                            gross - estimated_cost))
            except Exception as exc:
                errors.append(f"{buy.provider}->{sell_provider}: {exc}")
    if not possible:
        return None, errors
    return max(possible, key=lambda item: item.net_edge), errors
