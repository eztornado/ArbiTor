import argparse
import json
import logging
import signal
import time

from .config import load_config
from .arbitrage import find_best_opportunity, find_best_multichain_opportunity
from .laya import LayaRiskVeto
from .metrics import calculate
from .quotes import QuoteClient
from .store import Store
from .live import LiveExecutor, MultiChainLiveExecutor
from .discovery import TokenDiscovery
from .wizard import run_wizard


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("token-loop-monitor")
stopping = False


def stop(*_args):
    global stopping
    stopping = True


def main():
    global stopping
    parser = argparse.ArgumentParser(description="Escáner de arbitraje cross-venue y multichain para Base/BNB/Solana")
    parser.add_argument("--once", action="store_true", help="ejecuta un ciclo y termina")
    parser.add_argument("--live", action="store_true", help="ejecuta swaps reales (requiere LIVE_TRADING=true)")
    parser.add_argument("--metrics", action="store_true", help="muestra estadísticas históricas del par y termina")
    parser.add_argument("--discover", metavar="TOKEN", help="busca pools y representaciones del token por nombre, símbolo o address/mint")
    parser.add_argument("--wizard", nargs="?", const="", metavar="TOKEN",
                        help="asistente interactivo para buscar, revisar y configurar un token")
    parser.add_argument("--json-output", action="store_true", help="emite JSON (con --discover)")
    args = parser.parse_args()
    if args.metrics and args.live:
        parser.error("--metrics y --live son operaciones incompatibles")
    if args.discover and (args.live or args.metrics):
        parser.error("--discover es una consulta independiente y no se combina con --live o --metrics")
    if args.wizard is not None and (args.discover or args.live or args.metrics or args.once):
        parser.error("--wizard es interactivo y se ejecuta por separado")
    if args.json_output and not args.discover:
        parser.error("--json-output requiere --discover")
    if args.discover:
        candidates, errors = TokenDiscovery().discover(args.discover)
        if args.json_output:
            print(json.dumps({"query": args.discover, "candidates": [c.as_dict() for c in candidates],
                              "errors": errors}, ensure_ascii=False, indent=2))
        else:
            print(f"Descubrimiento de {args.discover}: {len(candidates)} pools candidatos")
            for item in candidates:
                liquidity = "n/d" if item.liquidity_usd is None else f"${item.liquidity_usd:,.0f}"
                volume = "n/d" if item.volume_24h_usd is None else f"${item.volume_24h_usd:,.0f}"
                price = "n/d" if item.price_usd is None else f"${item.price_usd:.8g}"
                support = "soportada" if item.execution_supported else "sin adaptador de trading"
                print(f"{item.chain:22} {item.token_symbol:12} {item.token_address} | "
                      f"DEX={item.dex} pool={item.pair_address} liq={liquidity} vol24h={volume} "
                      f"price={price} [{support}] fuentes={','.join(item.sources)}")
                if item.coingecko_coin_id:
                    print(f"  CoinGecko ID: {item.coingecko_coin_id}")
                if item.pair_url:
                    print(f"  {item.pair_url}")
            for error in errors:
                log.warning("Fuente de descubrimiento: %s", error)
            if not candidates:
                print("Sin pools coincidentes. Un símbolo no identifica un activo de forma única.")
        return
    if args.wizard is not None:
        run_wizard(args.wizard)
        return
    config = load_config()
    if args.live and not config.live_trading:
        raise RuntimeError("Para operar en real, configura LIVE_TRADING=true además de --live")
    market_configs = {chain: config.for_chain(chain) for chain in config.chains}
    clients = {chain: QuoteClient(market) for chain, market in market_configs.items()}
    client = clients[config.chain]
    live = (MultiChainLiveExecutor(config) if len(config.chains) > 1 else LiveExecutor(config)) if args.live else None
    store = Store(config.database_path)
    scope_chain = "+".join(config.chains) if len(config.chains) > 1 else config.chain
    metrics_quote = config.quote_asset if len(config.chains) > 1 else config.quote_token
    metrics_target = config.target_asset if len(config.chains) > 1 else config.target_token
    if args.metrics:
        history = store.recent_quote_pnls(scope_chain, metrics_quote, metrics_target,
                                           config.metrics_window)
        metrics = calculate(history, min_samples=config.metrics_min_samples,
                            min_win_probability=config.min_win_probability,
                            min_pnl=config.min_profit_quote)
        print(f"pair={scope_chain}:{metrics_quote}->{metrics_target}")
        print(f"quote_samples={metrics.samples} estimated_quote_win_probability={metrics.estimated_win_probability:.3f} "
              f"90pct_lower_bound={metrics.win_probability_lower_bound:.3f} "
              f"estimated_below_threshold_probability={metrics.estimated_below_threshold_probability:.3f}")
        print(f"mean_quote_pnl={metrics.mean_pnl:.2f} pnl_stddev={metrics.pnl_stddev:.2f} "
              f"conservative_pnl={metrics.conservative_pnl:.2f} risk_gate_ready={metrics.ready} "
              f"risk_gate_approved={metrics.approved}")
        realized = store.realized_live_pnls(scope_chain, metrics_quote, metrics_target)
        live_net = sum(realized)
        live_win_rate = (sum(value > 0 for value in realized) / len(realized)) if realized else 0.0
        equity, peak, max_drawdown = 0, 0, 0
        for value in realized:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        print(f"live_fills={len(realized)} live_win_rate={live_win_rate:.3f} "
              f"live_realized_pnl={live_net} live_max_drawdown={max_drawdown} quote_units")
        store.close()
        return

    laya = LayaRiskVeto(config) if config.laya_enabled else None
    if laya:
        try:
            log.info("Laya /health: %s", "ok" if laya.health() else "no disponible")
        except Exception as exc:
            log.warning("No respondió el health de Laya: %s", exc)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    stopping = False
    venue_summary = " | ".join(f"{chain}:{','.join(c.providers())}" for chain, c in clients.items())
    log.info("ARBITRAGE SCAN | mode=%s | venues=%s | chains=%s | quote=%s | target=%s | amount=%d",
             "live" if live else "paper", venue_summary,
             scope_chain, metrics_quote, metrics_target, config.amount_in)
    try:
        while not stopping:
            started = time.monotonic()
            try:
                if len(clients) > 1:
                    opportunity, quote_errors = find_best_multichain_opportunity(
                        clients,
                        {chain: market.quote_token for chain, market in market_configs.items()},
                        {chain: market.target_token for chain, market in market_configs.items()},
                        config.amount_in, config.estimated_cost_quote)
                else:
                    opportunity, quote_errors = find_best_opportunity(
                        client, config.quote_token, config.target_token,
                        config.amount_in, config.estimated_cost_quote)
                if opportunity is None:
                    store.record((scope_chain, "none", config.amount_in, 0, 0,
                                  config.estimated_cost_quote, 0, config.min_profit_quote,
                                  "no_cross_venue_route"), mode="live" if live else "paper", quote_pnl=0,
                                 quote_token=metrics_quote, target_token=metrics_target)
                    log.warning("Sin oportunidad cross-venue/multichain; venues=%s errores=%s",
                                venue_summary, quote_errors)
                    if args.once:
                        break
                    time.sleep(max(0.0, config.interval_seconds - (time.monotonic() - started)))
                    continue
                buy = type("QuoteView", (), {"provider": opportunity.buy_provider,
                                               "amount_out": opportunity.target_amount})()
                sell = type("QuoteView", (), {"provider": opportunity.sell_provider,
                                                "amount_out": opportunity.amount_out})()
                quote_pnl = opportunity.net_edge
                metrics = calculate(
                    store.recent_quote_pnls(scope_chain, metrics_quote, metrics_target,
                                            config.metrics_window),
                    min_samples=config.metrics_min_samples,
                    min_win_probability=config.min_win_probability,
                    min_pnl=config.min_profit_quote,
                )
                candidate = quote_pnl >= config.min_profit_quote
                status = "candidate" if candidate else "below_threshold"
                laya_probability = None
                laya_block = False
                laya_failed = False
                if laya and candidate and metrics.ready:
                    snapshot = {
                        "chain": f"{opportunity.buy_chain}->{opportunity.sell_chain}",
                        "quote_token": metrics_quote, "target_token": metrics_target,
                        "amount_in": config.amount_in,
                        "buy_out": buy.amount_out, "round_trip_out": sell.amount_out,
                        "buy_provider": opportunity.buy_provider,
                        "sell_provider": opportunity.sell_provider,
                        "estimated_net_pnl_quote_units": quote_pnl,
                        "history_samples": metrics.samples,
                        "historical_quote_win_probability": round(metrics.estimated_win_probability, 4),
                        "historical_quote_win_probability_lower_bound": round(metrics.win_probability_lower_bound, 4),
                        "historical_mean_pnl": round(metrics.mean_pnl, 2),
                        "historical_pnl_stddev": round(metrics.pnl_stddev, 2),
                        "conservative_historical_pnl": round(metrics.conservative_pnl, 2),
                    }
                    try:
                        laya_probability, laya_block = laya.assess(snapshot)
                        log.info("Laya veto probability=%.3f block=%s", laya_probability, laya_block)
                    except Exception:
                        laya_failed = True
                        log.exception("No se obtuvo evaluación Laya; el modo paper sigue con el filtro local")

                pnl = 0 if live else quote_pnl
                if live and candidate:
                    if store.live_pnl_today() <= -config.max_daily_loss:
                        status = "daily_loss_blocked"
                    elif store.live_trades_last_hour() >= config.max_trades_per_hour:
                        status = "hourly_limit_blocked"
                    elif not metrics.approved:
                        status = "metrics_blocked"
                    elif laya_block:
                        status = "laya_blocked"
                    elif laya and laya_failed:
                        status = "laya_unavailable"
                    else:
                        result = (live.execute_opportunity(opportunity, clients) if len(clients) > 1
                                  else live.execute_opportunity(opportunity, client))
                        pnl, status = result["pnl"], result["status"]
                        log.info("Ejecución live: %s | pnl=%d | %s",
                                 status, pnl, result["details"])
                cycle_id, total_pnl = store.record((
                    scope_chain, f"{opportunity.buy_chain}:{buy.provider}->{opportunity.sell_chain}:{sell.provider}", config.amount_in, buy.amount_out,
                    sell.amount_out, config.estimated_cost_quote, pnl,
                    config.min_profit_quote, status,
                ), mode="live" if live else "paper", quote_pnl=quote_pnl,
                   quote_token=metrics_quote, target_token=metrics_target)

                log.info("cycle=%d mode=%s status=%s buy=%s sell=%s in=%d mid=%d out=%d edge_or_realized_pnl=%d cumulative_pnl=%d quote_units",
                         cycle_id, "live" if live else "paper", status,
                         buy.provider, sell.provider, config.amount_in, buy.amount_out,
                         sell.amount_out, pnl, total_pnl)
                log.info("risk n=%d quote_edge_p=%.3f lower90=%.3f loss_est=%.3f mean=%.2f std=%.2f conservative=%.2f gate=%s laya_p=%s",
                         metrics.samples, metrics.estimated_win_probability,
                         metrics.win_probability_lower_bound, metrics.estimated_below_threshold_probability,
                         metrics.mean_pnl, metrics.pnl_stddev, metrics.conservative_pnl,
                         metrics.approved, "n/a" if laya_probability is None else f"{laya_probability:.3f}")
                if live and status in {"holding_after_buy", "reconciliation_required", "execution_state_unknown"}:
                    stopping = True
                    log.error("Ejecución parcial o incierta; daemon detenido. Reconcilia balances y transacciones antes de reanudar")
                if live and status in {"live_executed", "daily_loss_blocked"} and store.live_pnl_today() <= -config.max_daily_loss:
                    stopping = True
                    log.error("MAX_DAILY_LOSS alcanzado; daemon detenido")
            except Exception as exc:
                log.exception("No se pudo obtener/registrar ciclo: %s", exc)
                if live:
                    stopping = True
            if args.once or stopping:
                break
            time.sleep(max(0.0, config.interval_seconds - (time.monotonic() - started)))
    finally:
        store.close()


if __name__ == "__main__":
    main()
