import argparse
import logging
import signal
import time

from .config import load_config
from .execution import LiveExecutor
from .laya import LayaRiskVeto
from .metrics import calculate
from .quotes import QuoteClient
from .store import Store


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("token-loop-monitor")
stopping = False


def stop(*_args):
    global stopping
    stopping = True


def main():
    global stopping
    parser = argparse.ArgumentParser(description="Bot de swaps con filtro estadístico para Base/Solana")
    parser.add_argument("--once", action="store_true", help="ejecuta un ciclo y termina")
    parser.add_argument("--live", action="store_true", help="permite swaps reales (requiere LIVE_TRADING=true)")
    parser.add_argument("--metrics", action="store_true", help="muestra estadísticas históricas del par y termina")
    args = parser.parse_args()
    config = load_config()
    client, store = QuoteClient(config), Store(config.database_path)
    if args.metrics:
        history = store.recent_quote_pnls(config.chain, config.quote_token, config.target_token,
                                           config.metrics_window)
        metrics = calculate(history, min_samples=config.metrics_min_samples,
                            min_win_probability=config.min_win_probability,
                            min_pnl=config.min_profit_quote)
        print(f"pair={config.chain}:{config.quote_token}->{config.target_token}")
        print(f"quote_samples={metrics.samples} estimated_quote_win_probability={metrics.estimated_win_probability:.3f} "
              f"90pct_lower_bound={metrics.win_probability_lower_bound:.3f} "
              f"estimated_below_threshold_probability={metrics.estimated_below_threshold_probability:.3f}")
        print(f"mean_quote_pnl={metrics.mean_pnl:.2f} pnl_stddev={metrics.pnl_stddev:.2f} "
              f"conservative_pnl={metrics.conservative_pnl:.2f} risk_gate_ready={metrics.ready} "
              f"risk_gate_approved={metrics.approved}")
        realized = store.realized_live_pnls(config.chain, config.quote_token, config.target_token)
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

    live = LiveExecutor(config) if args.live else None
    if args.live and not config.live_trading:
        raise RuntimeError("Para operar en real configura LIVE_TRADING=true además de --live")
    laya = LayaRiskVeto(config) if config.laya_enabled else None
    if laya:
        try:
            log.info("Laya /health: %s", "ok" if laya.health() else "no disponible")
        except Exception as exc:
            log.warning("No respondió el health de Laya: %s", exc)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    stopping = False
    log.info("%s | chain=%s | quote=%s | target=%s | amount=%d",
             "LIVE MODE" if live else "PAPER MODE",
             config.chain, config.quote_token, config.target_token, config.amount_in)
    try:
        while not stopping:
            started = time.monotonic()
            try:
                buy = client.quote(config.quote_token, config.target_token, config.amount_in)
                sell = client.quote(config.target_token, config.quote_token, buy.amount_out)
                quote_pnl = sell.amount_out - config.amount_in - config.estimated_cost_quote
                metrics = calculate(
                    store.recent_quote_pnls(config.chain, config.quote_token, config.target_token,
                                            config.metrics_window),
                    min_samples=config.metrics_min_samples,
                    min_win_probability=config.min_win_probability,
                    min_pnl=config.min_profit_quote,
                )
                candidate = quote_pnl >= config.min_profit_quote
                status = "candidate" if candidate else "below_threshold"
                laya_probability = None
                laya_block = False
                if laya and candidate and metrics.ready:
                    snapshot = {
                        "chain": config.chain, "quote_token": config.quote_token,
                        "target_token": config.target_token, "amount_in": config.amount_in,
                        "buy_out": buy.amount_out, "round_trip_out": sell.amount_out,
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
                        if live:
                            raise
                        log.exception("No se obtuvo evaluación Laya; el modo paper sigue con el filtro local")

                pnl = quote_pnl
                if live and candidate:
                    if store.live_pnl_today() <= -config.max_daily_loss:
                        raise RuntimeError("Cortacircuitos: alcanzado MAX_DAILY_LOSS")
                    if store.live_trades_last_hour() >= config.max_trades_per_hour:
                        raise RuntimeError("Cortacircuitos: alcanzado MAX_TRADES_PER_HOUR")
                    if not metrics.approved:
                        status = "risk_blocked"
                    elif laya_block:
                        status = "laya_blocked"
                    else:
                        result = live.cycle(client)
                        pnl, status = result["pnl"], result["status"]
                        if status == "executed":
                            status = "live_executed"
                        log.info("Resultado live: %s | pnl=%d | %s", status, pnl, result["details"])

                stored_pnl = 0 if live and status not in {"live_executed", "holding_after_buy"} else pnl
                cycle_id, total_pnl = store.record((
                    config.chain, buy.provider, config.amount_in, buy.amount_out,
                    sell.amount_out, config.estimated_cost_quote, stored_pnl,
                    config.min_profit_quote, status,
                ), mode="live" if live else "paper", quote_pnl=quote_pnl,
                   quote_token=config.quote_token, target_token=config.target_token)
                if live and status == "live_executed" and store.live_pnl_today() <= -config.max_daily_loss:
                    stopping = True
                    log.error("Cortacircuitos: MAX_DAILY_LOSS alcanzado; daemon detenido")

                pnl_label = "realized_pnl" if live and status == "live_executed" else "estimated_pnl"
                log.info("cycle=%d mode=%s status=%s in=%d mid=%d out=%d %s=%d cumulative_%s_pnl=%d quote_units",
                         cycle_id, "live" if live else "paper", status,
                         config.amount_in, buy.amount_out, sell.amount_out,
                         pnl_label, pnl, "realized" if live else "estimated", total_pnl)
                log.info("risk n=%d quote_edge_p=%.3f lower90=%.3f loss_est=%.3f mean=%.2f std=%.2f conservative=%.2f gate=%s laya_p=%s",
                         metrics.samples, metrics.estimated_win_probability,
                         metrics.win_probability_lower_bound, metrics.estimated_below_threshold_probability,
                         metrics.mean_pnl, metrics.pnl_stddev, metrics.conservative_pnl,
                         metrics.approved, "n/a" if laya_probability is None else f"{laya_probability:.3f}")
                if live and status == "holding_after_buy":
                    stopping = True
                    log.error("Posición abierta tras la compra; daemon detenido para no aumentar exposición")
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
